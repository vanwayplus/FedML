import logging
import os

import requests


class Singleton(object):
    def __new__(cls):
        if not hasattr(cls, "_instance"):
            orig = super(Singleton, cls)
            cls._instance = orig.__new__(cls)
        return cls._instance


class MLOpsConfigs(Singleton):
    _config_instance = None

    def __init__(self):
        pass

    @staticmethod
    def get_instance(args):
        if MLOpsConfigs._config_instance is None:
            MLOpsConfigs._config_instance = MLOpsConfigs()
            MLOpsConfigs._config_instance.args = args

        return MLOpsConfigs._config_instance

    def get_request_params(self):
        url = "https://open.fedml.ai/fedmlOpsServer/configs/fetch"
        config_version = "release"
        if (
                hasattr(self.args, "config_version")
                and self.args.config_version is not None
        ):
            # Setup config url based on selected version.
            config_version = self.args.config_version
            if self.args.config_version == "release":
                url = "https://open.fedml.ai/fedmlOpsServer/configs/fetch"
            elif self.args.config_version == "test":
                url = "https://open-test.fedml.ai/fedmlOpsServer/configs/fetch"
            elif self.args.config_version == "dev":
                url = "https://open-dev.fedml.ai/fedmlOpsServer/configs/fetch"
            elif self.args.config_version == "local":
                if hasattr(self.args, "local_server") and self.args.local_server is not None:
                    url = "http://{}:9000/fedmlOpsServer/configs/fetch".format(self.args.local_server)
                else:
                    url = "http://localhost:9000/fedmlOpsServer/configs/fetch"

        cert_path = None
        if str(url).startswith("https://"):
            cur_source_dir = os.path.dirname(__file__)
            cert_path = os.path.join(
                cur_source_dir, "ssl", "open-" + config_version + ".fedml.ai_bundle.crt"
            )

        return url, cert_path

    def fetch_configs(self):
        url, cert_path = self.get_request_params()
        json_params = {"config_name": ["mqtt_config", "s3_config"]}
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
        if status_code == "SUCCESS":
            mqtt_config = response.json().get("data").get("mqtt_config")
            s3_config = response.json().get("data").get("s3_config")
        else:
            raise Exception("failed to fetch device configurations!")
        return mqtt_config, s3_config

    def fetch_all_configs(self):
        url, cert_path = self.get_request_params()
        logging.info(url)
        json_params = {"config_name": ["mqtt_config", "s3_config", "ml_ops_config", "docker_config"]}
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
        if status_code == "SUCCESS":
            mqtt_config = response.json().get("data").get("mqtt_config")
            s3_config = response.json().get("data").get("s3_config")
            mlops_config = response.json().get("data").get("ml_ops_config")
            docker_config = response.json().get("data").get("docker_config")
        else:
            raise Exception("failed to fetch device configurations!")

        return mqtt_config, s3_config, mlops_config, docker_config


if __name__ == "__main__":
    pass
