import io
import os
import platform

from setuptools import setup, find_packages


try:
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel


    class bdist_wheel(_bdist_wheel):
        def finalize_options(self):
            _bdist_wheel.finalize_options(self)
            self.root_is_pure = False


except ImportError:
    bdist_wheel = None

requirements = [
    "numpy",
    "PyYAML",
    "h5py",
    "tqdm",
    "wandb",
    "wget",
    "paho-mqtt",
    "boto3",
    "pynvml",
    "sklearn",
    "networkx",
    "click",
    "torch",
    "torchvision",
    "spacy",
    "gensim",
    "multiprocess",
]

requirements_extra_mpi = [
    "mpi4py",
]

if platform.machine() == "x86_64":
    requirements.append("MNN==1.1.6")

setup(
    name="fedml",
    version="0.7.218",
    author="FedML Team",
    author_email="ch@fedml.ai",
    description="A research and production integrated edge-cloud library for "
                "federated/distributed machine learning at anywhere at any scale.",
    long_description=io.open(os.path.join("README.md"), "r", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/FedML-AI/FedML",
    keywords=[
        "distributed machine learning",
        "federated learning",
        "natural language processing",
        "computer vision",
        "Internet of Things",
    ],
    classifiers=[
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX",
        "Operating System :: Unix",
        "Operating System :: MacOS",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
    ],
    packages=find_packages(),
    include_package_data=True,
    data_files=[
        (
            "fedml",
            [
                "fedml/config/simulation_sp/fedml_config.yaml",
                "fedml/config/simulaton_mpi/fedml_config.yaml",
                "fedml/cli/build-package/mlops-core/fedml-server/server-package/conf/fedml.yaml",
                "fedml/cli/build-package/mlops-core/fedml-server/server-package/fedml/config/fedml_config.yaml",
                "fedml/cli/build-package/mlops-core/fedml-client/client-package/conf/fedml.yaml",
                "fedml/cli/build-package/mlops-core/fedml-client/client-package/fedml/config/fedml_config.yaml",
                "fedml/cli/server_deployment/templates/fedml-aggregator-data-pv.yaml",
                "fedml/cli/server_deployment/templates/fedml-aggregator-data-pvc.yaml",
                "fedml/cli/server_deployment/templates/fedml-server-deployment.yaml",
                "fedml/cli/server_deployment/templates/fedml-server-svc.yaml",
                "fedml/core/mlops/ssl/open-dev.fedml.ai_bundle.crt",
                "fedml/core/mlops/ssl/open-test.fedml.ai_bundle.crt",
                "fedml/core/mlops/ssl/open-release.fedml.ai_bundle.crt",
            ],
        )
    ],
    install_requires=requirements,
    extras_require={
        "MPI": requirements_extra_mpi,
        "gRPC": "grpcio",
    },
    package_data={"": ["py.typed"]},
    license="Apache 2.0",
    entry_points={
        "console_scripts": [
            "fedml=fedml.cli.cli:cli",
        ]
    },
    cmdclass={"bdist_wheel": bdist_wheel},
)
