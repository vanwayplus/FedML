import json
import logging
import threading
import time

import wandb


class MLOpsProfilerEvent:
    EVENT_TYPE_STARTED = 0
    EVENT_TYPE_ENDED = 1

    _instance_lock = threading.Lock()
    _sys_perf_profiling = False
    _enable_wandb = False

    def __new__(cls, *args, **kwargs):
        if not hasattr(MLOpsProfilerEvent, "_instance"):
            with MLOpsProfilerEvent._instance_lock:
                if not hasattr(MLOpsProfilerEvent, "_instance"):
                    MLOpsProfilerEvent._instance = object.__new__(cls)
        return MLOpsProfilerEvent._instance

    def __init__(self, args):
        from ..distributed.communication.mqtt_s3.mqtt_s3_status_manager import (
            MqttS3StatusManager,
        )

        self.args = args
        self.enable_wandb = args.enable_wandb
        self.run_id = args.run_id
        if args.rank == 0:
            if hasattr(args, "server_id"):
                self.edge_id = args.server_id
            else:
                self.edge_id = 0
        else:
            if hasattr(args, "client_id"):
                self.edge_id = args.client_id
            elif hasattr(args, "client_id_list"):
                self.edge_id = json.loads(args.client_id_list)[0]
            else:
                self.edge_id = 0

        self.com_manager = MqttS3StatusManager(
            args.mqtt_config_path, args.s3_config_path, topic=args.run_id
        )

    @classmethod
    def enable_sys_perf_profiling(cls):
        cls._sys_perf_profiling = True

    @classmethod
    def enable_wandb(cls):
        cls._enable_wandb = True

    @classmethod
    def log_to_wandb(cls, metric):
        if cls._enable_wandb:
            wandb.log(metric)

    def log_event_started(self, event_name, event_value=None, event_edge_id=None):
        if event_value is None:
            event_value_passed = ""
        else:
            event_value_passed = event_value

        if event_edge_id is not None:
            edge_id = event_edge_id
        else:
            edge_id = self.edge_id

        event_topic, event_msg = self.__build_event_mqtt_msg(
            self.args.run_id,
            edge_id,
            MLOpsProfilerEvent.EVENT_TYPE_STARTED,
            event_name,
            event_value_passed,
        )
        event_msg_str = json.dumps(event_msg)
        logging.info("Event started, {}".format(event_msg_str))
        self.com_manager.send_message_json(event_topic, event_msg_str)

    def log_event_ended(self, event_name, event_value=None, event_edge_id=None):
        if event_value is None:
            event_value_passed = ""
        else:
            event_value_passed = event_value

        if event_edge_id is not None:
            edge_id = event_edge_id
        else:
            edge_id = self.edge_id

        event_topic, event_msg = self.__build_event_mqtt_msg(
            self.args.run_id,
            edge_id,
            MLOpsProfilerEvent.EVENT_TYPE_ENDED,
            event_name,
            event_value_passed,
        )
        event_msg_str = json.dumps(event_msg)
        logging.info("Event ended, {}".format(event_msg_str))
        self.com_manager.send_message_json(event_topic, event_msg_str)
        self.com_manager.send_message_json(event_topic, event_msg_str)

    @staticmethod
    def __build_event_mqtt_msg(run_id, edge_id, event_type, event_name, event_value):
        event_topic = "/mlops/events"
        event_msg = {}
        if event_type == MLOpsProfilerEvent.EVENT_TYPE_STARTED:
            event_msg = {
                "run_id": run_id,
                "edge_id": edge_id,
                "event_name": event_name,
                "event_value": event_value,
                "started_time": int(time.time()),
            }
        elif event_type == MLOpsProfilerEvent.EVENT_TYPE_ENDED:
            event_msg = {
                "run_id": run_id,
                "edge_id": edge_id,
                "event_name": event_name,
                "event_value": event_value,
                "ended_time": int(time.time()),
            }

        return event_topic, event_msg
