import json
import logging
import sys

import multiprocess as multiprocessing
import os
import platform
import re
import shutil
import stat
import subprocess
import threading

import time
import traceback
import urllib
import uuid
import zipfile

import click
import requests
from ...core.mlops.mlops_runtime_log import MLOpsRuntimeLog

from ...core.distributed.communication.mqtt.mqtt_manager import MqttManager
from ...cli.comm_utils.yaml_utils import load_yaml_config
from ...cli.edge_deployment.client_constants import ClientConstants

from ...core.mlops.mlops_metrics import MLOpsMetrics

from ...core.mlops.mlops_configs import MLOpsConfigs
from ...core.mlops.mlops_runtime_log_daemon import MLOpsRuntimeLogDaemon
from ...core.mlops.mlops_status import MLOpsStatus


class FedMLClientRunner:
    FEDML_BOOTSTRAP_RUN_OK = "[FedML]Bootstrap Finished"

    def __init__(self, args, edge_id=0, request_json=None, agent_config=None, run_id=0):
        self.device_status = None
        self.current_training_status = None
        self.mqtt_mgr = None
        self.client_mqtt_mgr = None
        self.client_mqtt_is_connected = False
        self.client_mqtt_lock = None
        self.edge_id = edge_id
        self.run_id = run_id
        self.unique_device_id = None
        self.process = None
        self.args = args
        self.request_json = request_json
        self.version = args.version
        self.device_id = args.device_id
        self.cloud_region = args.cloud_region
        self.cur_dir = os.path.split(os.path.realpath(__file__))[0]
        if args.current_running_dir is not None:
            self.cur_dir = args.current_running_dir
        self.sudo_cmd = ""
        self.is_mac = False
        if platform.system() == "Darwin":
            self.is_mac = True

        self.agent_config = agent_config
        self.fedml_data_base_package_dir = os.path.join("/", "fedml", "data")
        self.fedml_data_local_package_dir = os.path.join("/", "fedml", "fedml-package", "fedml", "data")
        self.fedml_data_dir = self.fedml_data_base_package_dir
        self.fedml_config_dir = os.path.join("/", "fedml", "conf")

        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES = {
            "${FEDSYS.RUN_ID}": "",
            "${FEDSYS.PRIVATE_LOCAL_DATA}": "",
            "${FEDSYS.CLIENT_ID_LIST}": "",
            "${FEDSYS.SYNTHETIC_DATA_URL}": "",
            "${FEDSYS.IS_USING_LOCAL_DATA}": "",
            "${FEDSYS.CLIENT_NUM}": "",
            "${FEDSYS.CLIENT_INDEX}": "",
            "${FEDSYS.CLIENT_OBJECT_LIST}": "",
            "${FEDSYS.LOG_SERVER_URL}": "",
        }

        self.mlops_metrics = None
        self.client_active_list = dict()
        # logging.info("Current directory of client agent: " + self.cur_dir)

    def build_dynamic_constrain_variables(self, run_id, run_config):
        data_config = run_config["data_config"]
        server_edge_id_list = self.request_json["edgeids"]
        local_edge_id_list = [1]
        local_edge_id_list[0] = self.edge_id
        is_using_local_data = 0
        private_data_dir = data_config["privateLocalData"]
        synthetic_data_url = data_config["syntheticDataUrl"]
        edges = self.request_json["edges"]
        # if private_data_dir is not None \
        #         and len(str(private_data_dir).strip(' ')) > 0:
        #     is_using_local_data = 1
        if private_data_dir is None or len(str(private_data_dir).strip(" ")) <= 0:
            params_config = run_config.get("parameters", None)
            private_data_dir = ClientConstants.get_data_dir()
        if synthetic_data_url is None or len(str(synthetic_data_url)) <= 0:
            synthetic_data_url = private_data_dir

        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.RUN_ID}"] = run_id
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.PRIVATE_LOCAL_DATA}"] = private_data_dir.replace(" ", "")
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_ID_LIST}"] = str(local_edge_id_list).replace(" ", "")
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.SYNTHETIC_DATA_URL}"] = synthetic_data_url.replace(" ", "")
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.IS_USING_LOCAL_DATA}"] = str(is_using_local_data)
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_NUM}"] = len(server_edge_id_list)
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_INDEX}"] = server_edge_id_list.index(self.edge_id) + 1
        client_objects = str(json.dumps(edges))
        client_objects = client_objects.replace(" ", "").replace("\n", "").replace('"', '\\"')
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_OBJECT_LIST}"] = client_objects
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.LOG_SERVER_URL}"] = self.agent_config["ml_ops_config"][
            "LOG_SERVER_URL"
        ]

    def unzip_file(self, zip_file, unzip_file_path):
        result = False
        if zipfile.is_zipfile(zip_file):
            with zipfile.ZipFile(zip_file, "r") as zipf:
                zipf.extractall(unzip_file_path)
                result = True

        return result

    def retrieve_and_unzip_package(self, package_name, package_url):
        local_package_path = ClientConstants.get_package_download_dir()
        try:
            os.makedirs(local_package_path)
        except Exception as e:
            pass
        local_package_file = os.path.join(local_package_path, os.path.basename(package_url))
        if not os.path.exists(local_package_file):
            urllib.request.urlretrieve(package_url, local_package_file)
        unzip_package_path = ClientConstants.get_package_unzip_dir()
        try:
            shutil.rmtree(ClientConstants.get_package_run_dir(package_name), ignore_errors=True)
        except Exception as e:
            pass
        self.unzip_file(local_package_file, unzip_package_path)
        unzip_package_path = ClientConstants.get_package_run_dir(package_name)
        return unzip_package_path

    def update_local_fedml_config(self, run_id, run_config):
        packages_config = run_config["packages_config"]

        # Copy config file from the client
        unzip_package_path = self.retrieve_and_unzip_package(
            packages_config["linuxClient"], packages_config["linuxClientUrl"]
        )
        fedml_local_config_file = os.path.join(unzip_package_path, "conf", "fedml.yaml")

        # Load the above config to memory
        config_from_container = load_yaml_config(fedml_local_config_file)
        container_entry_file_config = config_from_container["entry_config"]
        container_dynamic_args_config = config_from_container["dynamic_args"]
        entry_file = container_entry_file_config["entry_file"]
        conf_file = container_entry_file_config["conf_file"]
        full_conf_path = os.path.join(unzip_package_path, "fedml", "config", os.path.basename(conf_file))

        # Dynamically build constrain variable with realtime parameters from server
        self.build_dynamic_constrain_variables(run_id, run_config)

        # Update entry arguments value with constrain variable values with realtime parameters from server
        # currently we support the following constrain variables:
        # ${FEDSYS_RUN_ID}: a run id represented one entire Federated Learning flow
        # ${FEDSYS_PRIVATE_LOCAL_DATA}: private local data path in the Federated Learning client
        # ${FEDSYS_CLIENT_ID_LIST}: client list in one entire Federated Learning flow
        # ${FEDSYS_SYNTHETIC_DATA_URL}: synthetic data url from server,
        #                  if this value is not null, the client will download data from this URL to use it as
        #                  federated training data set
        # ${FEDSYS_IS_USING_LOCAL_DATA}: whether use private local data as federated training data set
        # container_dynamic_args_config["data_cache_dir"] = "${FEDSYS.PRIVATE_LOCAL_DATA}"
        for constrain_variable_key, constrain_variable_value in self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES.items():
            for argument_key, argument_value in container_dynamic_args_config.items():
                if argument_value is not None and str(argument_value).find(constrain_variable_key) == 0:
                    replaced_argument_value = str(argument_value).replace(
                        constrain_variable_key, str(constrain_variable_value)
                    )
                    container_dynamic_args_config[argument_key] = replaced_argument_value

        # Merge all container new config sections as new config dictionary
        package_conf_object = dict()
        package_conf_object["entry_config"] = container_entry_file_config
        package_conf_object["dynamic_args"] = container_dynamic_args_config
        package_conf_object["dynamic_args"]["config_version"] = self.args.config_version
        container_dynamic_args_config["mqtt_config_path"] = os.path.join(
            unzip_package_path, "fedml", "config", os.path.basename(container_dynamic_args_config["mqtt_config_path"])
        )
        container_dynamic_args_config["s3_config_path"] = os.path.join(
            unzip_package_path, "fedml", "config", os.path.basename(container_dynamic_args_config["s3_config_path"])
        )
        log_file_dir = ClientConstants.get_log_file_dir()
        try:
            os.makedirs(log_file_dir)
        except Exception as e:
            pass
        package_conf_object["dynamic_args"]["log_file_dir"] = log_file_dir

        # Save new config dictionary to local file
        fedml_updated_config_file = os.path.join(unzip_package_path, "conf", "fedml.yaml")
        ClientConstants.generate_yaml_doc(package_conf_object, fedml_updated_config_file)

        # Build dynamic arguments and set arguments to fedml config object
        self.build_dynamic_args(run_config, package_conf_object, unzip_package_path)

        return unzip_package_path, package_conf_object

    def build_dynamic_args(self, run_config, package_conf_object, base_dir):
        fedml_conf_file = package_conf_object["entry_config"]["conf_file"]
        fedml_conf_path = os.path.join(base_dir, "fedml", "config", os.path.basename(fedml_conf_file))
        fedml_conf_object = load_yaml_config(fedml_conf_path)

        # Replace local fedml config objects with parameters from MLOps web
        parameters_object = run_config.get("parameters", None)
        if parameters_object is not None:
            fedml_conf_object = parameters_object

        package_dynamic_args = package_conf_object["dynamic_args"]
        fedml_conf_object["comm_args"]["mqtt_config_path"] = package_dynamic_args["mqtt_config_path"]
        fedml_conf_object["comm_args"]["s3_config_path"] = package_dynamic_args["s3_config_path"]
        fedml_conf_object["common_args"]["using_mlops"] = True
        fedml_conf_object["train_args"]["run_id"] = package_dynamic_args["run_id"]
        fedml_conf_object["train_args"]["client_id_list"] = package_dynamic_args["client_id_list"]
        fedml_conf_object["train_args"]["client_num_in_total"] = int(package_dynamic_args["client_num_in_total"])
        fedml_conf_object["train_args"]["client_num_per_round"] = int(package_dynamic_args["client_num_in_total"])
        fedml_conf_object["train_args"]["client_id"] = self.edge_id
        fedml_conf_object["train_args"]["server_id"] = self.request_json.get("server_id", "0")
        fedml_conf_object["device_args"]["worker_num"] = int(package_dynamic_args["client_num_in_total"])
        # fedml_conf_object["data_args"]["data_cache_dir"] = package_dynamic_args["data_cache_dir"]
        fedml_conf_object["tracking_args"]["log_file_dir"] = package_dynamic_args["log_file_dir"]
        fedml_conf_object["tracking_args"]["log_server_url"] = package_dynamic_args["log_server_url"]
        if hasattr(self.args, "local_server") and self.args.local_server is not None:
            fedml_conf_object["comm_args"]["local_server"] = self.args.local_server
        bootstrap_script_file = fedml_conf_object["environment_args"]["bootstrap"]
        bootstrap_script_dir = os.path.join(base_dir, "fedml", os.path.dirname(bootstrap_script_file))
        bootstrap_script_path = os.path.join(
            bootstrap_script_dir, bootstrap_script_dir, os.path.basename(bootstrap_script_file)
        )
        # try:
        #     os.makedirs(package_dynamic_args["data_cache_dir"])
        # except Exception as e:
        #     pass
        fedml_conf_object["dynamic_args"] = package_dynamic_args

        ClientConstants.generate_yaml_doc(fedml_conf_object, fedml_conf_path)

        try:
            if bootstrap_script_path is not None:
                if os.path.exists(bootstrap_script_path):
                    bootstrap_stat = os.stat(bootstrap_script_path)
                    os.chmod(bootstrap_script_path, bootstrap_stat.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                bootstrap_scripts = "cd {}; ./{}".format(bootstrap_script_dir, os.path.basename(bootstrap_script_file))
                logging.info("Bootstrap scripts are being executed...")
                process = ClientConstants.exec_console_with_script(bootstrap_scripts, should_capture_stdout_err=True)
                ret_code, out, err = ClientConstants.get_console_pipe_out_err_results(process)
                if out is not None:
                    out_str = out.decode(encoding="utf-8")
                    if str(out_str).find(FedMLClientRunner.FEDML_BOOTSTRAP_RUN_OK) == -1:
                        logging.error("{}".format(out_str))
                    else:
                        logging.info("{}".format(out_str))
                if err is not None:
                    err_str = err.decode(encoding="utf-8")
                    if str(err_str).find(FedMLClientRunner.FEDML_BOOTSTRAP_RUN_OK) == -1:
                        logging.error("{}".format(err_str))
                    else:
                        logging.info("{}".format(err_str))
        except Exception as e:
            logging.error("Bootstrap scripts error: {}".format(traceback.format_exc()))

    def build_image_unique_id(self, run_id, run_config):
        config_name = str(run_config.get("configName", "run_" + str(run_id)))
        config_creater = str(run_config.get("userId", "user_" + str(run_id)))
        image_unique_id = re.sub("[^a-zA-Z0-9_-]", "", str(config_name + "_" + config_creater))
        image_unique_id = image_unique_id.lower()
        return image_unique_id

    def run(self):
        run_id = self.request_json["runId"]
        run_config = self.request_json["run_config"]
        data_config = run_config["data_config"]
        packages_config = run_config["packages_config"]

        MLOpsRuntimeLog.get_instance(self.args).init_logs(show_stdout_log=True)

        self.setup_client_mqtt_mgr()
        self.wait_client_mqtt_connected()

        self.mlops_metrics.report_client_training_status(self.edge_id,
                                                         ClientConstants.MSG_MLOPS_CLIENT_STATUS_INITIALIZING)

        # get training params
        private_local_data_dir = data_config.get("privateLocalData", "")
        is_using_local_data = 0
        # if private_local_data_dir is not None and len(str(private_local_data_dir).strip(' ')) > 0:
        #     is_using_local_data = 1

        # start a run according to the hyper-parameters
        # fedml_local_data_dir = self.cur_dir + "/fedml_data/run_" + str(run_id) + "_edge_" + str(edge_id)
        fedml_local_data_dir = os.path.join(self.cur_dir, "fedml_data")
        fedml_local_config_dir = os.path.join(self.cur_dir, "fedml_config")
        if is_using_local_data:
            fedml_local_data_dir = private_local_data_dir
        self.fedml_data_dir = self.fedml_data_local_package_dir

        # update local config with real time parameters from server and dynamically replace variables value
        unzip_package_path, fedml_config_object = self.update_local_fedml_config(run_id, run_config)

        entry_file_config = fedml_config_object["entry_config"]
        dynamic_args_config = fedml_config_object["dynamic_args"]
        entry_file = os.path.basename(entry_file_config["entry_file"])
        conf_file = entry_file_config["conf_file"]
        ClientConstants.cleanup_learning_process()
        os.chdir(os.path.join(unzip_package_path, "fedml"))

        python_program = "python"
        python_version_str = os.popen("python --version").read()
        if python_version_str.find("Python 3.") == -1:
            python_version_str = os.popen("python3 --version").read()
            if python_version_str.find("Python 3.") != -1:
                python_program = "python3"

        # process = subprocess.Popen([python_program, entry_file,
        #                             '--cf', conf_file, '--rank', str(dynamic_args_config["rank"]),
        #                             '--role', 'client'])

        process = ClientConstants.exec_console_with_shell_script_list(
            [
                python_program,
                entry_file,
                "--cf",
                conf_file,
                "--rank",
                str(dynamic_args_config["rank"]),
                "--role",
                "client",
            ]
        )
        ClientConstants.save_learning_process(process.pid)
        self.release_client_mqtt_mgr()
        ret_code, out, err = ClientConstants.get_console_sys_out_pipe_err_results(process)
        if ret_code != 0 and err is not None and str(err.decode(encoding="utf-8")).find('__finish ') == -1:
            logging.error("Exception when executing client program: {}".format(err.decode(encoding="utf-8")))
            self.setup_client_mqtt_mgr()
            self.wait_client_mqtt_connected()
            self.mlops_metrics.report_client_id_status(run_id, self.edge_id,
                                                       ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED)
            self.release_client_mqtt_mgr()

    def reset_devices_status(self, edge_id, status):
        self.mlops_metrics.run_id = self.run_id
        self.mlops_metrics.edge_id = edge_id
        self.mlops_metrics.broadcast_client_training_status(edge_id, status)

    def stop_run(self):
        self.setup_client_mqtt_mgr()

        self.wait_client_mqtt_connected()

        logging.info("Stop run successfully.")

        # Stop log processor for current run
        MLOpsRuntimeLogDaemon.get_instance(self.args).stop_log_processor(self.run_id, self.edge_id)

        time.sleep(2)

        # Notify MLOps with the stopping message
        self.mlops_metrics.report_client_training_status(self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_STOPPING)

        self.reset_devices_status(self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED)

        time.sleep(1)

        try:
            ClientConstants.cleanup_learning_process()
        except Exception as e:
            pass

        self.release_client_mqtt_mgr()

    def exit_run_with_exception(self):
        self.setup_client_mqtt_mgr()

        self.wait_client_mqtt_connected()

        logging.info("Exit run successfully.")

        ClientConstants.cleanup_learning_process()
        ClientConstants.cleanup_run_process()

        # Notify MLOps with the stopping message
        self.mlops_metrics.report_client_id_status(self.run_id, self.edge_id,
                                                   ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED)

        time.sleep(1)

        self.release_client_mqtt_mgr()

    def cleanup_run_when_starting_failed(self):
        self.setup_client_mqtt_mgr()

        self.wait_client_mqtt_connected()

        logging.info("Cleanup run successfully when starting failed.")

        # Stop log processor for current run
        MLOpsRuntimeLogDaemon.get_instance(self.args).stop_log_processor(self.run_id, self.edge_id)

        time.sleep(2)

        self.reset_devices_status(self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED)

        time.sleep(2)

        try:
            self.mlops_metrics.set_sys_reporting_status(False)
        except Exception as ex:
            pass

        time.sleep(1)

        try:
            ClientConstants.cleanup_learning_process()
        except Exception as e:
            pass

        self.release_client_mqtt_mgr()

    def cleanup_run_when_finished(self):
        self.setup_client_mqtt_mgr()

        self.wait_client_mqtt_connected()

        logging.info("Cleanup run successfully when finished.")

        # Stop log processor for current run
        MLOpsRuntimeLogDaemon.get_instance(self.args).stop_log_processor(self.run_id, self.edge_id)

        time.sleep(2)

        self.reset_devices_status(self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED)

        time.sleep(2)

        try:
            self.mlops_metrics.set_sys_reporting_status(False)
        except Exception as ex:
            pass

        time.sleep(1)

        try:
            ClientConstants.cleanup_learning_process()
        except Exception as e:
            pass

        self.release_client_mqtt_mgr()

    def on_client_mqtt_disconnected(self, mqtt_client_object):
        if self.client_mqtt_lock is None:
            self.client_mqtt_lock = threading.Lock()

        self.client_mqtt_lock.acquire()
        self.client_mqtt_is_connected = False
        self.client_mqtt_lock.release()

    def on_client_mqtt_connected(self, mqtt_client_object):
        if self.mlops_metrics is None:
            self.mlops_metrics = MLOpsMetrics()

        self.mlops_metrics.set_messenger(self.client_mqtt_mgr)
        self.mlops_metrics.run_id = self.run_id

        if self.client_mqtt_lock is None:
            self.client_mqtt_lock = threading.Lock()

        self.client_mqtt_lock.acquire()
        self.client_mqtt_is_connected = True
        self.client_mqtt_lock.release()

    def setup_client_mqtt_mgr(self):
        if self.client_mqtt_lock is None:
            self.client_mqtt_lock = threading.Lock()
        if self.client_mqtt_mgr is not None:
            self.client_mqtt_lock.acquire()
            self.client_mqtt_mgr.remove_disconnected_listener(self.on_client_mqtt_disconnected)
            self.client_mqtt_is_connected = False
            self.client_mqtt_mgr.disconnect()
            self.client_mqtt_mgr = None
            self.client_mqtt_lock.release()

        self.client_mqtt_mgr = MqttManager(
            self.agent_config["mqtt_config"]["BROKER_HOST"],
            self.agent_config["mqtt_config"]["BROKER_PORT"],
            self.agent_config["mqtt_config"]["MQTT_USER"],
            self.agent_config["mqtt_config"]["MQTT_PWD"],
            self.agent_config["mqtt_config"]["MQTT_KEEPALIVE"],
            "ClientAgent_Comm_Client" + str(uuid.uuid4()),
        )

        self.client_mqtt_mgr.add_connected_listener(self.on_client_mqtt_connected)
        self.client_mqtt_mgr.add_disconnected_listener(self.on_client_mqtt_disconnected)
        self.client_mqtt_mgr.connect()
        self.client_mqtt_mgr.loop_start()

    def release_client_mqtt_mgr(self):
        if self.client_mqtt_mgr is not None:
            self.client_mqtt_mgr.disconnect()
            self.client_mqtt_mgr.loop_stop()

        self.client_mqtt_lock.acquire()
        if self.client_mqtt_mgr is not None:
            self.client_mqtt_is_connected = False
            self.client_mqtt_mgr = None
        self.client_mqtt_lock.release()

    def wait_client_mqtt_connected(self):
        while True:
            self.client_mqtt_lock.acquire()
            if self.client_mqtt_is_connected is True:
                self.client_mqtt_lock.release()
                break
            self.client_mqtt_lock.release()
            time.sleep(1)

    def callback_start_train(self, topic, payload):
        # get training params
        request_json = json.loads(payload)
        run_id = request_json["runId"]

        # Terminate previous process about starting or stopping run command
        ClientConstants.exit_process(self.process)
        ClientConstants.cleanup_run_process()
        ClientConstants.save_runner_infos(self.args.device_id + "." + self.args.os_name, self.edge_id, run_id=run_id)

        # Start log processor for current run
        self.args.run_id = run_id
        MLOpsRuntimeLogDaemon.get_instance(self.args).start_log_processor(run_id, self.edge_id)

        # Start cross-silo server with multi processing mode
        self.request_json = request_json
        client_runner = FedMLClientRunner(
            self.args, edge_id=self.edge_id, request_json=request_json, agent_config=self.agent_config, run_id=run_id
        )
        self.process = multiprocessing.Process(target=client_runner.run)
        self.process.start()
        ClientConstants.save_run_process(self.process.pid)

    def callback_stop_train(self, topic, payload):
        logging.info("callback_stop_train: topic = %s, payload = %s" % (topic, payload))

        request_json = json.loads(payload)
        run_id = request_json["runId"]

        logging.info("Stopping run...")
        logging.info("Stop run with multiprocessing.")

        # Stop cross-silo server with multi processing mode
        self.request_json = request_json
        client_runner = FedMLClientRunner(
            self.args, edge_id=self.edge_id, request_json=request_json, agent_config=self.agent_config, run_id=run_id
        )
        try:
            multiprocessing.Process(target=client_runner.stop_run).start()
        except Exception as e:
            pass

    def callback_exit_train_with_exception(self, topic, payload):
        logging.info("callback_exit_train_with_exception: topic = %s, payload = %s" % (topic, payload))

        request_json = json.loads(payload)
        run_id = request_json["runId"]

        logging.info("Exit run...")
        logging.info("Exit run with multiprocessing.")

        # Stop cross-silo server with multi processing mode
        self.request_json = request_json
        client_runner = FedMLClientRunner(
            self.args, edge_id=self.edge_id, request_json=request_json, agent_config=self.agent_config, run_id=run_id
        )
        try:
            multiprocessing.Process(target=client_runner.exit_run_with_exception).start()
        except Exception as e:
            pass

    def cleanup_client_with_status(self):
        if self.device_status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED:
            self.cleanup_run_when_finished()
        elif self.device_status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED:
            self.cleanup_run_when_starting_failed()

    def callback_runner_id_status(self, topic, payload):
        logging.info("callback_runner_id_status: topic = %s, payload = %s" % (topic, payload))

        request_json = json.loads(payload)
        run_id = request_json["run_id"]
        edge_id = request_json["edge_id"]
        status = request_json["status"]

        self.save_training_status(edge_id, status)

        if status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED or \
                status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED:
            logging.info("Received training status message.")
            logging.info("Will end training client.")

            # Stop cross-silo server with multi processing mode
            self.request_json = request_json
            client_runner = FedMLClientRunner(
                self.args,
                edge_id=self.edge_id,
                request_json=request_json,
                agent_config=self.agent_config,
                run_id=run_id,
            )
            client_runner.device_status = status
            multiprocessing.Process(target=client_runner.cleanup_client_with_status).start()

    def report_client_status(self):
        self.send_agent_active_msg()

    def callback_report_current_status(self, topic, payload):
        self.send_agent_active_msg()

    def callback_client_last_will_msg(self, topic, payload):
        msg = json.loads(payload)
        edge_id = msg.get("ID", None)
        status = msg.get("status", ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE)
        if edge_id is not None and status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE:
            if self.client_active_list.get(edge_id, None) is not None:
                self.client_active_list.pop(edge_id)

    def callback_client_active_msg(self, topic, payload):
        msg = json.loads(payload)
        edge_id = msg.get("ID", None)
        status = msg.get("status", ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE)
        if edge_id is not None:
            self.client_active_list[edge_id] = status

    def save_training_status(self, edge_id, training_status):
        self.current_training_status = training_status
        ClientConstants.save_training_infos(edge_id, training_status)

    @staticmethod
    def get_device_id():
        if "nt" in os.name:

            def GetUUID():
                cmd = "wmic csproduct get uuid"
                uuid = str(subprocess.check_output(cmd))
                pos1 = uuid.find("\\n") + 2
                uuid = uuid[pos1:-15]
                return str(uuid)

            device_id = str(GetUUID())
            logging.info(device_id)
        elif "posix" in os.name:
            device_id = hex(uuid.getnode())
        else:
            device_id = subprocess.Popen(
                "hal-get-property --udi /org/freedesktop/Hal/devices/computer --key system.hardware.uuid".split()
            )
            device_id = hex(device_id)

        return device_id

    def bind_account_and_device_id(self, url, account_id, device_id, os_name, role="client"):
        json_params = {
            "accountid": account_id,
            "deviceid": device_id,
            "type": os_name,
            "gpu": "None",
            "processor": "",
            "network": "",
            "role": role,
        }
        _, cert_path = MLOpsConfigs.get_instance(self.args).get_request_params()
        if cert_path is not None:
            requests.session().verify = cert_path
            response = requests.post(url, json=json_params, verify=True, headers={"Connection": "close"})
        else:
            response = requests.post(url, json=json_params, headers={"Connection": "close"})
        status_code = response.json().get("code")
        if status_code == "SUCCESS":
            edge_id = response.json().get("data").get("id")
        else:
            return 0
        return edge_id

    def fetch_configs(self):
        return MLOpsConfigs.get_instance(self.args).fetch_all_configs()

    def send_agent_active_msg(self):
        active_topic = "/flclient_agent/active"
        status = MLOpsStatus.get_instance().get_client_agent_status(self.edge_id)
        if (
            status is not None
            and status != ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE
            and status != ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE
        ):
            return
        status = ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE
        active_msg = {"ID": self.edge_id, "status": status}
        MLOpsStatus.get_instance().set_client_agent_status(self.edge_id, status)
        self.mqtt_mgr.send_message_json(active_topic, json.dumps(active_msg))

    def on_agent_mqtt_connected(self, mqtt_client_object):
        # The MQTT message topic format is as follows: <sender>/<receiver>/<action>

        # Init the mlops metrics object
        if self.mlops_metrics is None:
            self.mlops_metrics = MLOpsMetrics()
            self.mlops_metrics.set_messenger(self.mqtt_mgr)
            self.mlops_metrics.report_client_training_status(self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE)
            MLOpsStatus.get_instance().set_client_agent_status(
                self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE
            )

        # Setup MQTT message listener for starting training
        topic_start_train = "flserver_agent/" + str(self.edge_id) + "/start_train"
        self.mqtt_mgr.add_message_listener(topic_start_train, self.callback_start_train)

        # Setup MQTT message listener for stopping training
        topic_stop_train = "flserver_agent/" + str(self.edge_id) + "/stop_train"
        self.mqtt_mgr.add_message_listener(topic_stop_train, self.callback_stop_train)

        # Setup MQTT message listener for running failed
        topic_exit_train_with_exception = "flserver_agent/" + str(self.edge_id) + "/exit_train_with_exception"
        self.mqtt_mgr.add_message_listener(topic_exit_train_with_exception, self.callback_exit_train_with_exception)

        # Setup MQTT message listener for client status switching
        topic_client_status = "fl_client/flclient_agent_" + str(self.edge_id) + "/status"
        self.mqtt_mgr.add_message_listener(topic_client_status, self.callback_runner_id_status)

        # Setup MQTT message listener to report current device status.
        topic_report_status = "/mlops/report_device_status"
        self.mqtt_mgr.add_message_listener(topic_report_status, self.callback_report_current_status)

        # Setup MQTT message listener to the last will message form the client.
        topic_last_will_msg = "/flclient/last_will_msg"
        self.mqtt_mgr.add_message_listener(topic_last_will_msg, self.callback_client_last_will_msg)

        # Setup MQTT message listener to the active status message form the client.
        topic_active_msg = "/flclient/active"
        self.mqtt_mgr.add_message_listener(topic_active_msg, self.callback_client_active_msg)

        # Subscribe topics for starting train, stopping train and fetching client status.
        mqtt_client_object.subscribe(topic_start_train)
        mqtt_client_object.subscribe(topic_stop_train)
        mqtt_client_object.subscribe(topic_client_status)
        mqtt_client_object.subscribe(topic_report_status)
        mqtt_client_object.subscribe(topic_last_will_msg)
        mqtt_client_object.subscribe(topic_active_msg)
        mqtt_client_object.subscribe(topic_exit_train_with_exception)

        # Broadcast the first active message.
        self.send_agent_active_msg()

        # Echo results
        click.echo("")
        click.echo("Congratulations, you have logged into the FedML MLOps platform successfully!")
        click.echo(
            "Your device id is "
            + str(self.unique_device_id)
            + ". You may review the device in the MLOps edge device list."
        )

    def on_agent_mqtt_disconnected(self, mqtt_client_object):
        MLOpsStatus.get_instance().set_client_agent_status(
            self.edge_id, ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE
        )
        pass

    def setup_agent_mqtt_connection(self, service_config):
        # Setup MQTT connection
        self.mqtt_mgr = MqttManager(
            service_config["mqtt_config"]["BROKER_HOST"],
            service_config["mqtt_config"]["BROKER_PORT"],
            service_config["mqtt_config"]["MQTT_USER"],
            service_config["mqtt_config"]["MQTT_PWD"],
            service_config["mqtt_config"]["MQTT_KEEPALIVE"],
            self.edge_id,
            "/flclient_agent/last_will_msg",
            json.dumps({"ID": self.edge_id, "status": ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE}),
        )
        self.agent_config = service_config

        # Setup MQTT connected listener
        self.mqtt_mgr.add_connected_listener(self.on_agent_mqtt_connected)
        self.mqtt_mgr.add_disconnected_listener(self.on_agent_mqtt_disconnected)
        self.mqtt_mgr.connect()

    def start_agent_mqtt_loop(self):
        # Start MQTT message loop
        while True:
            try:
                self.mqtt_mgr.loop_forever()
            except Exception as e:
                self.mqtt_mgr.connect()
                time.sleep(1)
