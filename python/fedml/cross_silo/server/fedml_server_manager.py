import json
import logging
import time

from .message_define import MyMessage
from ...core.distributed.communication.message import Message
from ...core.distributed.server.server_manager import ServerManager
from ...core.mlops.mlops_metrics import MLOpsMetrics
from ...core.mlops.mlops_profiler_event import MLOpsProfilerEvent


class FedMLServerManager(ServerManager):
    def __init__(
        self,
        args,
        aggregator,
        comm=None,
        client_rank=0,
        client_num=0,
        backend="MQTT_S3",
    ):
        super().__init__(args, comm, client_rank, client_num, backend)
        self.args = args
        self.aggregator = aggregator
        self.round_num = args.comm_round
        self.round_idx = 0

        self.client_online_mapping = {}
        self.client_real_ids = json.loads(args.client_id_list)

        if hasattr(self.args, "using_mlops") and self.args.using_mlops:
            self.mlops_metrics = MLOpsMetrics()
            self.mlops_metrics.set_messenger(self.com_manager_status, self.args)
            self.mlops_event = MLOpsProfilerEvent(self.args)
            self.aggregator.set_mlops_logger(self.mlops_metrics)

        self.start_running_time = None
        self.aggregated_model_url = None

        self.is_initialized = False
        self.client_id_list_in_this_round = None
        self.data_silo_index_list = None

    def run(self):
        self.start_running_time = time.time()
        super().run()

    def send_init_msg(self):
        global_model_params = self.aggregator.get_global_model_params()

        client_idx_in_this_round = 0
        for client_id in self.client_id_list_in_this_round:
            self.send_message_init_config(
                client_id,
                global_model_params,
                self.data_silo_index_list[client_idx_in_this_round],
            )
            client_idx_in_this_round += 1

        if hasattr(self.args, "using_mlops") and self.args.using_mlops:
            self.mlops_event.log_event_started(
                "server.wait", event_value=str(self.round_idx)
            )

    def register_message_receive_handlers(self):
        logging.info("register_message_receive_handlers------")
        self.register_message_receive_handler(
            MyMessage.MSG_TYPE_CONNECTION_IS_READY, self.handle_messag_connection_ready
        )

        self.register_message_receive_handler(
            MyMessage.MSG_TYPE_C2S_CLIENT_STATUS,
            self.handle_message_client_status_update,
        )

        self.register_message_receive_handler(
            MyMessage.MSG_TYPE_C2S_SEND_MODEL_TO_SERVER,
            self.handle_message_receive_model_from_client,
        )

    def handle_messag_connection_ready(self, msg_params):
        logging.info("Connection is ready!")
        logging.info("self.client_real_ids = {}".format(self.client_real_ids))
        self.client_id_list_in_this_round = self.aggregator.client_selection(
            self.round_idx, self.client_real_ids, self.args.client_num_per_round
        )
        self.data_silo_index_list = self.aggregator.data_silo_selection(
            self.round_idx,
            self.args.client_num_in_total,
            len(self.client_id_list_in_this_round),
        )
        if not self.is_initialized:
            # check client status in case that some clients start earlier than the server
            client_idx_in_this_round = 0
            for client_id in self.client_id_list_in_this_round:
                try:
                    self.send_message_check_client_status(
                        client_id,
                        self.data_silo_index_list[client_idx_in_this_round],
                    )
                    logging.info("Connection ready for client" + str(client_id))
                except Exception as e:
                    logging.info("Connection not ready for client" + str(client_id))
                client_idx_in_this_round += 1

    def handle_message_client_status_update(self, msg_params):
        client_status = msg_params.get(MyMessage.MSG_ARG_KEY_CLIENT_STATUS)
        if client_status == "ONLINE":
            self.client_online_mapping[str(msg_params.get_sender_id())] = True

            logging.info("self.client_online_mapping = {}".format(self.client_online_mapping))

        # notify MLOps with RUNNING status
        if hasattr(self.args, "using_mlops") and self.args.using_mlops:
            self.mlops_metrics.report_server_training_status(
                self.args.run_id, MyMessage.MSG_MLOPS_SERVER_STATUS_RUNNING,
                role="server"
            )

        all_client_is_online = True
        for client_id in self.client_id_list_in_this_round:
            if not self.client_online_mapping.get(str(client_id), False):
                all_client_is_online = False
                break

        logging.info(
            "sender_id = %d, all_client_is_online = %s"
            % (msg_params.get_sender_id(), str(all_client_is_online))
        )

        if all_client_is_online:
            # send initialization message to all clients to start training
            self.send_init_msg()
            self.is_initialized = True

    def handle_message_receive_model_from_client(self, msg_params):
        sender_id = msg_params.get(MyMessage.MSG_ARG_KEY_SENDER)
        if hasattr(self.args, "using_mlops") and self.args.using_mlops:
            self.mlops_event.log_event_ended(
                "comm_c2s", event_value=str(self.round_idx), event_edge_id=sender_id
            )

        model_params = msg_params.get(MyMessage.MSG_ARG_KEY_MODEL_PARAMS)
        local_sample_number = msg_params.get(MyMessage.MSG_ARG_KEY_NUM_SAMPLES)

        self.aggregator.add_local_trained_result(
            self.client_real_ids.index(sender_id), model_params, local_sample_number
        )
        b_all_received = self.aggregator.check_whether_all_receive()
        logging.info("b_all_received = " + str(b_all_received))
        if b_all_received:
            if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                self.mlops_event.log_event_ended(
                    "server.wait", event_value=str(self.round_idx)
                )
                self.mlops_event.log_event_started(
                    "server.agg_and_eval", event_value=str(self.round_idx)
                )
            tick = time.time()
            global_model_params = self.aggregator.aggregate()
            MLOpsProfilerEvent.log_to_wandb({"AggregationTime": time.time() - tick, 'round': self.round_idx })

            try:
                self.aggregator.test_on_server_for_all_clients(self.round_idx)
            except Exception as e:
                logging.info("aggregator.test exception: " + str(e))

            if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                self.mlops_event.log_event_ended(
                    "server.agg_and_eval", event_value=str(self.round_idx)
                )

            # send round info to the MQTT backend
            if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                round_info = {
                    "run_id": self.args.run_id,
                    "round_index": self.round_idx,
                    "total_rounds": self.round_num,
                    "running_time": round(time.time() - self.start_running_time, 4),
                }
                if self.mlops_metrics is not None:
                    self.mlops_metrics.report_server_training_round_info(round_info)

            self.client_id_list_in_this_round = self.aggregator.client_selection(
                self.round_idx, self.client_real_ids, self.args.client_num_per_round
            )
            self.data_silo_index_list = self.aggregator.data_silo_selection(
                self.round_idx,
                self.args.client_num_in_total,
                len(self.client_id_list_in_this_round),
            )

            if self.round_idx == 0:
                MLOpsProfilerEvent.log_to_wandb({"BenchmarkStart": time.time()})

            client_idx_in_this_round = 0
            for receiver_id in self.client_id_list_in_this_round:
                self.send_message_sync_model_to_client(
                    receiver_id,
                    global_model_params,
                    self.data_silo_index_list[client_idx_in_this_round],
                )
                client_idx_in_this_round += 1

            if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                model_info = {
                    "run_id": self.args.run_id,
                    "round_idx": self.round_idx + 1,
                    "global_aggregated_model_s3_address": self.aggregated_model_url,
                }
                self.mlops_metrics.report_aggregated_model_info(model_info)
                self.aggregated_model_url = None

            self.round_idx += 1
            if self.round_idx == self.round_num:
                # post_complete_message_to_sweep_process(self.args)
                if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                    self.mlops_metrics.report_server_id_status(
                        self.args.run_id, MyMessage.MSG_MLOPS_SERVER_STATUS_FINISHED
                    )
                logging.info(
                    "=============training is finished. Cleanup...============"
                )
                self.cleanup()
            else:
                logging.info("\n\n==========start {}-th round training===========\n".format(self.round_idx))
                if hasattr(self.args, "using_mlops") and self.args.using_mlops:
                    self.mlops_event.log_event_started(
                        "server.wait", event_value=str(self.round_idx)
                    )

    def cleanup(self):

        client_idx_in_this_round = 0
        for client_id in self.client_id_list_in_this_round:
            self.send_message_finish(
                client_id, self.data_silo_index_list[client_idx_in_this_round],
            )
            client_idx_in_this_round += 1
        time.sleep(3)
        self.finish()

    def send_message_init_config(self, receive_id, global_model_params, datasilo_index):
        tick = time.time()
        message = Message(MyMessage.MSG_TYPE_S2C_INIT_CONFIG, self.get_sender_id(), receive_id)
        message.add_params(MyMessage.MSG_ARG_KEY_MODEL_PARAMS, global_model_params)
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_INDEX, str(datasilo_index))
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_OS, "PythonClient")
        self.send_message(message)
        MLOpsProfilerEvent.log_to_wandb({"Communiaction/Send_Total": time.time() - tick})

    def send_message_check_client_status(self, receive_id, datasilo_index):
        message = Message(
            MyMessage.MSG_TYPE_S2C_CHECK_CLIENT_STATUS, self.get_sender_id(), receive_id
        )
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_INDEX, str(datasilo_index))
        self.send_message(message)

    def send_message_finish(self, receive_id, datasilo_index):
        message = Message(
            MyMessage.MSG_TYPE_S2C_FINISH, self.get_sender_id(), receive_id
        )
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_INDEX, str(datasilo_index))
        self.send_message(message)
        logging.info(
            " ====================send cleanup message to {}====================".format(
                str(datasilo_index)
            )
        )

    def send_message_sync_model_to_client(
        self, receive_id, global_model_params, client_index
    ):
        tick = time.time()
        logging.info("send_message_sync_model_to_client. receive_id = %d" % receive_id)
        message = Message(
            MyMessage.MSG_TYPE_S2C_SYNC_MODEL_TO_CLIENT,
            self.get_sender_id(),
            receive_id,
        )
        message.add_params(MyMessage.MSG_ARG_KEY_MODEL_PARAMS, global_model_params)
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_INDEX, str(client_index))
        message.add_params(MyMessage.MSG_ARG_KEY_CLIENT_OS, "PythonClient")
        self.send_message(message)

        MLOpsProfilerEvent.log_to_wandb({"Communiaction/Send_Total": time.time() - tick})

        if self.aggregated_model_url is None and self.args.backend == "MQTT_S3":
            self.aggregated_model_url = message.get(MyMessage.MSG_ARG_KEY_MODEL_PARAMS_URL)
