import json
import logging
import os
import platform
import threading
import time
import uuid

import click
import requests
from ...cli.cli import mlops_register_simulator_process
from ...cli.edge_deployment.client_constants import ClientConstants
from ...cli.edge_deployment.client_runner import FedMLClientRunner
from ...constants import FEDML_TRAINING_PLATFORM_SIMULATION, FEDML_TRAINING_PLATFORM_SIMULATION_TYPE
from ...cli.server_deployment.server_constants import ServerConstants

from ..distributed.communication.mqtt.mqtt_manager import MqttManager

from .mlops_configs import MLOpsConfigs

from .mlops_metrics import MLOpsMetrics
from .mlops_profiler_event import MLOpsProfilerEvent
from .mlops_runtime_log import MLOpsRuntimeLog
from .system_stats import SysStats
from .mlops_runtime_log_daemon import MLOpsRuntimeLogProcessor
from .mlops_runtime_log_daemon import MLOpsRuntimeLogDaemon
from .mlops_status import MLOpsStatus


FEDML_MLOPS_API_RESPONSE_SUCCESS_CODE = "SUCCESS"

__all__ = [
    "MLOpsMetrics",
    "MLOpsProfilerEvent",
    "MLOpsRuntimeLog",
    "MLOpsRuntimeLogProcessor",
    "MLOpsRuntimeLogDaemon",
    "SysStats",
    "MLOpsStatus",
]


class MLOpsStore:
    mlops_args = None
    mlops_project_id: int = None
    mlops_run_id = None
    mlops_edge_id = None
    mlops_log_metrics = dict()
    mlops_log_round_info = dict()
    mlops_log_client_training_status = ClientConstants.MSG_MLOPS_CLIENT_STATUS_TRAINING
    mlops_log_server_training_status = ServerConstants.MSG_MLOPS_SERVER_STATUS_RUNNING
    mlops_log_round_start_time = 0.0
    mlops_log_metrics_lock = None
    mlops_log_mqtt_mgr = None
    mlops_log_mqtt_lock = None
    mlops_log_mqtt_is_connected = False
    mlops_log_agent_config = None
    mlops_metrics = None
    mlops_bind_result = False

    def __init__(self):
        pass


def init(args):
    MLOpsStore.mlops_args = args
    if not mlops_tracking_enabled(args):
        MLOpsRuntimeLog.get_instance(args).init_logs()
        return
    project_name = None
    api_key = None
    run_name = None
    if hasattr(args, "mlops_project_name"):
        project_name = args.mlops_project_name
    if hasattr(args, "mlops_api_key"):
        api_key = args.mlops_api_key
    if hasattr(args, "mlops_run_name"):
        run_name = args.mlops_run_name
    if project_name is None or api_key is None:
        raise Exception("Please check mlops_project_name and mlops_api_key params.")

    # Bind local device as simulation device on the MLOps platform.
    setattr(args, "using_mlops", True)
    MLOpsStore.mlops_bind_result = bind_local_device(args, api_key, args.config_version)
    if not MLOpsStore.mlops_bind_result:
        setattr(args, "using_mlops", False)
        MLOpsRuntimeLog.get_instance(args).init_logs()
        return

    # Init project and run
    result_project, project_id = create_project(project_name, api_key)
    if result_project:
        result_run, run_id = create_run(project_id, api_key, run_name)
        if result_run:
            MLOpsStore.mlops_project_id = project_id
            MLOpsStore.mlops_run_id = run_id
    if result_project is False or result_run is False:
        click.echo("Failed to init project and run.")
        return

    # Init runtime logs
    init_logs(MLOpsStore.mlops_args, MLOpsStore.mlops_edge_id)
    logging.info("mlops.init args {}".format(MLOpsStore.mlops_args))

    # Start simulator login process as daemon
    #mlops_simulator_login(api_key)


def event(event_name, event_started=True, event_value=None, event_edge_id=None):
    if not mlops_tracking_enabled(MLOpsStore.mlops_args):
        return

    if not MLOpsStore.mlops_bind_result:
        return

    mlops_event = MLOpsProfilerEvent(MLOpsStore.mlops_args)
    mlops_event.run_id = MLOpsStore.mlops_run_id
    mlops_event.edge_id = MLOpsStore.mlops_edge_id
    if event_started:
        mlops_event.log_event_started(event_name, event_value, event_edge_id)
    else:
        mlops_event.log_event_ended(event_name, event_value, event_edge_id)


def log(metrics: dict, commit=True):
    if not mlops_tracking_enabled(MLOpsStore.mlops_args):
        return

    if not MLOpsStore.mlops_bind_result:
        return

    if MLOpsStore.mlops_log_metrics_lock is None:
        MLOpsStore.mlops_log_metrics_lock = threading.Lock()

    MLOpsStore.mlops_log_metrics_lock.acquire()
    for k, v in metrics.items():
        k = str(k).replace("/", "_")
        if k.startswith("round"):
            k = "round_idx"
        MLOpsStore.mlops_log_metrics[k] = v
    MLOpsStore.mlops_log_metrics["run_id"] = str(MLOpsStore.mlops_run_id)
    MLOpsStore.mlops_log_metrics["timestamp"] = time.time()
    MLOpsStore.mlops_log_metrics_lock.release()

    logging.info("log metrics {}".format(json.dumps(MLOpsStore.mlops_log_metrics)))

    if commit:
        setup_log_mqtt_mgr()
        wait_log_mqtt_connected()
        MLOpsStore.mlops_log_metrics_lock.acquire()
        MLOpsStore.mlops_metrics.report_server_training_metric(MLOpsStore.mlops_log_metrics)
        MLOpsStore.mlops_log_metrics.clear()
        MLOpsStore.mlops_log_metrics_lock.release()
        release_log_mqtt_mgr()


def log_training_status(status):
    if not mlops_tracking_enabled(MLOpsStore.mlops_args):
        return

    if not MLOpsStore.mlops_bind_result:
        return

    logging.info("log training status {}".format(status))

    setup_log_mqtt_mgr()
    wait_log_mqtt_connected()
    MLOpsStore.mlops_metrics.report_client_training_status(MLOpsStore.mlops_edge_id, status)
    release_log_mqtt_mgr()


def log_aggregation_status(status):
    if not mlops_tracking_enabled(MLOpsStore.mlops_args):
        return

    if not MLOpsStore.mlops_bind_result:
        return

    logging.info("log aggregation status {}".format(status))

    setup_log_mqtt_mgr()
    wait_log_mqtt_connected()
    MLOpsStore.mlops_metrics.report_server_training_status(MLOpsStore.mlops_run_id, status, role="simulator")
    release_log_mqtt_mgr()


def log_round_info(total_rounds, round_index):
    if not mlops_tracking_enabled(MLOpsStore.mlops_args):
        return

    if not MLOpsStore.mlops_bind_result:
        return

    if round_index == 0:
        MLOpsStore.mlops_log_round_start_time = time.time()

    setup_log_mqtt_mgr()
    wait_log_mqtt_connected()
    round_info = {
        "run_id": MLOpsStore.mlops_run_id,
        "round_index": round_index,
        "total_rounds": total_rounds,
        "running_time": round(time.time() - MLOpsStore.mlops_log_round_start_time, 4),
    }
    logging.info("log round info {}".format(round_info))
    MLOpsStore.mlops_metrics.report_server_training_round_info(round_info)
    release_log_mqtt_mgr()

    if round_index == total_rounds:
        mlops_simulator_logout()


def create_project(project_name, api_key):
    url_prefix, cert_path = get_request_params(MLOpsStore.mlops_args)
    url = "{}/fedmlOpsServer/projects/createSim".format(url_prefix)
    json_params = {"name": project_name,
                   "userids": api_key,
                   "platform_type": str(FEDML_TRAINING_PLATFORM_SIMULATION_TYPE)}
    if cert_path is not None:
        requests.session().verify = cert_path
        response = requests.post(
            url, json=json_params, verify=True, headers={"Connection": "close"}
        )
    else:
        response = requests.post(
            url, json=json_params, headers={"Connection": "close"}
        )
    status_code = response.json().get("code")
    if status_code == FEDML_MLOPS_API_RESPONSE_SUCCESS_CODE:
        project_id = response.json().get("data")
        return True, project_id
    else:
        return False, 0


def create_run(project_id, api_key, run_name=None):
    url_prefix, cert_path = get_request_params(MLOpsStore.mlops_args)
    url = "{}/fedmlOpsServer/runs/createSim".format(url_prefix)
    edge_ids = list()
    edge_ids.append(MLOpsStore.mlops_edge_id)
    json_params = {"userids": api_key,
                   "projectid": str(project_id),
                   "edgeids": edge_ids}
    if run_name is not None:
        json_params["name"] = run_name
    if cert_path is not None:
        requests.session().verify = cert_path
        response = requests.post(
            url, json=json_params, verify=True, headers={"Connection": "close"}
        )
    else:
        response = requests.post(
            url, json=json_params, headers={"Connection": "close"}
        )
    status_code = response.json().get("code")
    if status_code == FEDML_MLOPS_API_RESPONSE_SUCCESS_CODE:
        run_id = response.json().get("data")
        return True, run_id
    else:
        return False, 0


def get_request_params(args):
    url = "https://open.fedml.ai"
    config_version = "release"
    if (
            hasattr(args, "config_version")
            and args.config_version is not None
    ):
        # Setup config url based on selected version.
        config_version = args.config_version
        if args.config_version == "release":
            url = "https://open.fedml.ai"
        elif args.config_version == "test":
            url = "https://open-test.fedml.ai"
        elif args.config_version == "dev":
            url = "https://open-dev.fedml.ai"
        elif args.config_version == "local":
            if hasattr(args, "local_server") and args.local_server is not None:
                url = "http://{}:9000".format(args.local_server)
            else:
                url = "http://localhost:9000"

    cert_path = None
    if str(url).startswith("https://"):
        cur_source_dir = os.path.dirname(__file__)
        cert_path = os.path.join(
            cur_source_dir, "ssl", "open-" + config_version + ".fedml.ai_bundle.crt"
        )

    return url, cert_path


def on_log_mqtt_disconnected(mqtt_client_object):
    if MLOpsStore.mlops_log_mqtt_lock is None:
        MLOpsStore.mlops_log_mqtt_lock = threading.Lock()

    MLOpsStore.mlops_log_mqtt_lock.acquire()
    MLOpsStore.mlops_log_mqtt_is_connected = False
    MLOpsStore.mlops_log_mqtt_lock.release()

    #logging.info("on_client_mqtt_disconnected: {}.".format(MLOpsStore.mlops_log_mqtt_is_connected))


def on_log_mqtt_connected(mqtt_client_object):
    if MLOpsStore.mlops_metrics is None:
        MLOpsStore.mlops_metrics = MLOpsMetrics()

    MLOpsStore.mlops_metrics.set_messenger(MLOpsStore.mlops_log_mqtt_mgr)
    MLOpsStore.mlops_metrics.run_id = MLOpsStore.mlops_run_id
    MLOpsStore.mlops_metrics.edge_id = MLOpsStore.mlops_edge_id

    if MLOpsStore.mlops_log_mqtt_lock is None:
        MLOpsStore.mlops_log_mqtt_lock = threading.Lock()

    MLOpsStore.mlops_log_mqtt_lock.acquire()
    MLOpsStore.mlops_log_mqtt_is_connected = True
    MLOpsStore.mlops_log_mqtt_lock.release()

    #logging.info("on_client_mqtt_connected: {}.".format(MLOpsStore.mlops_log_mqtt_is_connected))


def setup_log_mqtt_mgr():
    if MLOpsStore.mlops_log_mqtt_lock is None:
        MLOpsStore.mlops_log_mqtt_lock = threading.Lock()

    if MLOpsStore.mlops_log_mqtt_mgr is not None:
        MLOpsStore.mlops_log_mqtt_lock.acquire()
        MLOpsStore.mlops_log_mqtt_mgr.remove_disconnected_listener(on_log_mqtt_disconnected)
        MLOpsStore.mlops_log_mqtt_is_connected = False
        MLOpsStore.mlops_log_mqtt_mgr.disconnect()
        MLOpsStore.mlops_log_mqtt_mgr = None
        MLOpsStore.mlops_log_mqtt_lock.release()

    #logging.info("mlops log metrics agent config: {},{}".format(MLOpsStore.mlops_log_agent_config["mqtt_config"]["BROKER_HOST"],
    #                                                 MLOpsStore.mlops_log_agent_config["mqtt_config"]["BROKER_PORT"]))

    MLOpsStore.mlops_log_mqtt_mgr = MqttManager(
        MLOpsStore.mlops_log_agent_config["mqtt_config"]["BROKER_HOST"],
        MLOpsStore.mlops_log_agent_config["mqtt_config"]["BROKER_PORT"],
        MLOpsStore.mlops_log_agent_config["mqtt_config"]["MQTT_USER"],
        MLOpsStore.mlops_log_agent_config["mqtt_config"]["MQTT_PWD"],
        MLOpsStore.mlops_log_agent_config["mqtt_config"]["MQTT_KEEPALIVE"],
        "Simulation_Link_" + str(uuid.uuid4()),
        )
    MLOpsStore.mlops_log_mqtt_mgr.add_connected_listener(on_log_mqtt_connected)
    MLOpsStore.mlops_log_mqtt_mgr.add_disconnected_listener(on_log_mqtt_disconnected)
    MLOpsStore.mlops_log_mqtt_mgr.connect()
    MLOpsStore.mlops_log_mqtt_mgr.loop_start()


def release_log_mqtt_mgr():
    if MLOpsStore.mlops_log_mqtt_mgr is not None:
        MLOpsStore.mlops_log_mqtt_mgr.disconnect()
        MLOpsStore.mlops_log_mqtt_mgr.loop_stop()

    MLOpsStore.mlops_log_mqtt_lock.acquire()
    if MLOpsStore.mlops_log_mqtt_mgr is not None:
        MLOpsStore.mlops_log_mqtt_is_connected = False
        MLOpsStore.mlops_log_mqtt_mgr = None
    MLOpsStore.mlops_log_mqtt_lock.release()


def wait_log_mqtt_connected():
    while True:
        MLOpsStore.mlops_log_mqtt_lock.acquire()
        if MLOpsStore.mlops_log_mqtt_is_connected is True:
            MLOpsStore.mlops_log_mqtt_lock.release()
            break
        MLOpsStore.mlops_log_mqtt_lock.release()
        time.sleep(1)


def init_logs(args, edge_id):
    # Init runtime logs
    args.log_file_dir = ClientConstants.get_log_file_dir()
    args.run_id = MLOpsStore.mlops_run_id
    args.rank = 1
    client_ids = list()
    client_ids.append(edge_id)
    args.client_id_list = json.dumps(client_ids)
    MLOpsRuntimeLog.get_instance(args).init_logs()
    logging.info("client ids:{}".format(args.client_id_list))


def bind_local_device(args, userid, version="release"):
    setattr(args, "account_id", userid)
    setattr(args, "current_running_dir", ClientConstants.get_fedml_home_dir())

    sys_name = platform.system()
    if sys_name == "Darwin":
        sys_name = "MacOS"
    setattr(args, "os_name", sys_name)
    setattr(args, "version", version)
    setattr(args, "log_file_dir", ClientConstants.get_log_file_dir())
    setattr(args, "device_id", FedMLClientRunner.get_device_id())
    setattr(args, "config_version", version)
    setattr(args, "cloud_region", "")

    # Create client runner for communication with the FedML server.
    runner = FedMLClientRunner(args)

    # Fetch configs from the MLOps config server.
    service_config = dict()
    config_try_count = 0
    edge_id = 0
    while config_try_count < 5:
        try:
            mqtt_config, s3_config, mlops_config, docker_config = runner.fetch_configs()
            service_config["mqtt_config"] = mqtt_config
            service_config["s3_config"] = s3_config
            service_config["ml_ops_config"] = mlops_config
            service_config["docker_config"] = docker_config
            runner.agent_config = service_config
            MLOpsStore.mlops_log_agent_config = service_config
            setattr(args, "mqtt_config_path", mqtt_config)
            setattr(args, "s3_config_path", s3_config)
            setattr(args, "log_server_url", mlops_config["LOG_SERVER_URL"])
            break
        except Exception as e:
            config_try_count += 1
            time.sleep(0.5)
            continue

    if config_try_count >= 5:
        click.echo("\nNote: Internet is not connected. "
                   "Experimental tracking results will not be synchronized to the MLOps (open.fedml.ai).\n")
        return False

    # Build unique device id
    if args.device_id is not None and len(str(args.device_id)) > 0:
        unique_device_id = args.device_id + "@" + args.os_name + ".Edge.Simulator"

    # Bind account id to the MLOps platform.
    register_try_count = 0
    edge_id = 0
    while register_try_count < 5:
        try:
            edge_id = runner.bind_account_and_device_id(
                service_config["ml_ops_config"]["EDGE_BINDING_URL"],
                args.account_id, unique_device_id, args.os_name,
                role="simulator"
            )
            if edge_id > 0:
                runner.edge_id = edge_id
                break
        except Exception as e:
            register_try_count += 1
            time.sleep(3)
            continue

    if edge_id <= 0:
        click.echo("Oops, you failed to login the FedML MLOps platform.")
        click.echo("Please check whether your network is normal!")
        return False
    MLOpsStore.mlops_edge_id = edge_id

    # Log arguments and binding results.
    runner.unique_device_id = unique_device_id

    MLOpsStore.mlops_args = args

    return True


def mlops_simulator_login(userid):
    login_simulator_cmd = "fedml login {} -c -r edge_simulator".format(userid)
    os.system(login_simulator_cmd)

    mlops_register_simulator_process(os.getpid(), ClientConstants.login_role_list[ClientConstants.LOGIN_MODE_EDGE_SIMULATOR_INDEX])


def mlops_simulator_logout():
    exit(-1)


def mlops_tracking_enabled(args):
    if (
            hasattr(args, "enable_tracking")
            and args.enable_tracking is True
            and args.training_type == FEDML_TRAINING_PLATFORM_SIMULATION
    ):
        return True
    else:
        return False