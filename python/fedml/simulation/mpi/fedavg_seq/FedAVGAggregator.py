import copy
import logging
import random
import time

import numpy as np
import torch
import wandb

from .utils import transform_list_to_tensor


class FedAVGAggregator(object):
    def __init__(
        self,
        train_global,
        test_global,
        all_train_data_num,
        train_data_local_dict,
        test_data_local_dict,
        train_data_local_num_dict,
        worker_num,
        device,
        args,
        model_trainer,
    ):
        self.trainer = model_trainer

        self.args = args
        self.train_global = train_global
        self.test_global = test_global
        self.val_global = self._generate_validation_set()
        self.all_train_data_num = all_train_data_num

        self.train_data_local_dict = train_data_local_dict
        self.test_data_local_dict = test_data_local_dict
        self.train_data_local_num_dict = train_data_local_num_dict

        self.worker_num = worker_num
        self.device = device
        self.model_dict = dict()
        self.sample_num_dict = dict()
        self.flag_client_model_uploaded_dict = dict()
        for idx in range(self.worker_num):
            self.flag_client_model_uploaded_dict[idx] = False

    def get_global_model_params(self):
        return self.trainer.get_model_params()

    def set_global_model_params(self, model_parameters):
        self.trainer.set_model_params(model_parameters)

    def add_local_trained_result(self, index, model_params):
        logging.info("add_model. index = %d" % index)
        self.model_dict[index] = model_params
        # self.sample_num_dict[index] = sample_num
        self.flag_client_model_uploaded_dict[index] = True

    def check_whether_all_receive(self):
        logging.debug("worker_num = {}".format(self.worker_num))
        for idx in range(self.worker_num):
            if not self.flag_client_model_uploaded_dict[idx]:
                return False
        for idx in range(self.worker_num):
            self.flag_client_model_uploaded_dict[idx] = False
        return True

    def workload_estimate(self, client_indexes, mode="simulate"):
        if mode == "simulate":
            client_samples = [
                self.train_data_local_num_dict[client_index]
                for client_index in client_indexes
            ]
            workload = client_samples
        elif mode == "real":
            raise NotImplementedError
        else:
            raise NotImplementedError
        return workload

    def memory_estimate(self, client_indexes, mode="simulate"):
        if mode == "simulate":
            memory = np.ones(self.worker_num)
        elif mode == "real":
            raise NotImplementedError
        else:
            raise NotImplementedError
        return memory

    def resource_estimate(self, mode="simulate"):
        if mode == "simulate":
            resource = np.ones(self.worker_num)
        elif mode == "real":
            raise NotImplementedError
        else:
            raise NotImplementedError
        return resource

    def record_client_runtime(self, worker_id, client_runtimes):
        pass

    def client_schedule(self, round_idx, client_indexes, mode="simulate"):
        # scheduler(workloads, constraints, memory)
        # workload = self.workload_estimate(client_indexes, mode)
        # resource = self.resource_estimate(mode)
        # memory = self.memory_estimate(mode)

        # mode = 0
        # my_scheduler = scheduler(workload, resource, memory)
        # schedules = my_scheduler.DP_schedule(mode)
        # for i in range(len(schedules)):
        #     print("Resource %2d: %s\n" % (i, str(schedules[i])))

        client_schedule = np.array_split(client_indexes, self.worker_num)
        return client_schedule

    def get_average_weight(self, client_indexes):
        average_weight_dict = {}
        training_num = 0
        for client_index in client_indexes:
            training_num += self.train_data_local_num_dict[client_index]

        for client_index in client_indexes:
            average_weight_dict[client_index] = (
                self.train_data_local_num_dict[client_index] / training_num
            )
        return average_weight_dict

    def aggregate(self):
        start_time = time.time()
        model_list = []
        training_num = 0

        for idx in range(self.worker_num):
            if self.args.is_mobile == 1:
                self.model_dict[idx] = transform_list_to_tensor(self.model_dict[idx])

            # added for attack & defense; enable multiple defenses
            # if FedMLDefender.get_instance().is_defense_enabled():
            #     self.model_dict[idx] = FedMLDefender.get_instance().defend(
            #         self.model_dict[idx], self.get_global_model_params()
            #     )

            # model_list.append((self.sample_num_dict[idx], self.model_dict[idx]))
            model_list.append(self.model_dict[idx])
            # training_num += self.sample_num_dict[idx]
        logging.info("len of self.model_dict[idx] = " + str(len(self.model_dict)))

        # logging.info("################aggregate: %d" % len(model_list))
        # (num0, averaged_params) = model_list[0]
        averaged_params = model_list[0]
        for k in averaged_params.keys():
            for i in range(0, len(model_list)):
                local_model_params = model_list[i]
                # w = local_sample_number / training_num
                if i == 0:
                    averaged_params[k] = local_model_params[k]
                else:
                    averaged_params[k] += local_model_params[k]

        # update the global model which is cached at the server side
        self.set_global_model_params(averaged_params)

        end_time = time.time()
        logging.info("aggregate time cost: %d" % (end_time - start_time))
        return averaged_params

    def client_sampling(self, round_idx, client_num_in_total, client_num_per_round):
        if client_num_in_total == client_num_per_round:
            client_indexes = [
                client_index for client_index in range(client_num_in_total)
            ]
        else:
            num_clients = min(client_num_per_round, client_num_in_total)
            np.random.seed(
                round_idx
            )  # make sure for each comparison, we are selecting the same clients each round
            client_indexes = np.random.choice(
                range(client_num_in_total), num_clients, replace=False
            )
        logging.info("client_indexes = %s" % str(client_indexes))
        return client_indexes

    def _generate_validation_set(self, num_samples=10000):
        if self.args.dataset.startswith("stackoverflow"):
            test_data_num = len(self.test_global.dataset)
            sample_indices = random.sample(
                range(test_data_num), min(num_samples, test_data_num)
            )
            subset = torch.utils.data.Subset(self.test_global.dataset, sample_indices)
            sample_testset = torch.utils.data.DataLoader(
                subset, batch_size=self.args.batch_size
            )
            return sample_testset
        else:
            return self.test_global

    def test_on_server_for_all_clients(self, round_idx):
        if self.trainer.test_on_the_server(
            self.train_data_local_dict,
            self.test_data_local_dict,
            self.device,
            self.args,
        ):
            return

        if (
            round_idx % self.args.frequency_of_the_test == 0
            or round_idx == self.args.comm_round - 1
        ):
            logging.info(
                "################test_on_server_for_all_clients : {}".format(round_idx)
            )
            train_num_samples = []
            train_tot_corrects = []
            train_losses = []
            # for client_idx in range(self.args.client_num_in_total):
            #     # train data
            #     metrics = self.trainer.test(
            #         self.train_data_local_dict[client_idx], self.device, self.args
            #     )
            #     train_tot_correct, train_num_sample, train_loss = (
            #         metrics["test_correct"],
            #         metrics["test_total"],
            #         metrics["test_loss"],
            #     )
            #     train_tot_corrects.append(copy.deepcopy(train_tot_correct))
            #     train_num_samples.append(copy.deepcopy(train_num_sample))
            #     train_losses.append(copy.deepcopy(train_loss))

            # test on training dataset
            # train_acc = sum(train_tot_corrects) / sum(train_num_samples)
            # train_loss = sum(train_losses) / sum(train_num_samples)
            # if self.args.enable_wandb:
            #     wandb.log({"Train/Acc": train_acc, "round": round_idx})
            #     wandb.log({"Train/Loss": train_loss, "round": round_idx})
            # stats = {"training_acc": train_acc, "training_loss": train_loss}
            # logging.info(stats)

            # test data
            test_num_samples = []
            test_tot_corrects = []
            test_losses = []

            if round_idx == self.args.comm_round - 1:
                metrics = self.trainer.test(self.test_global, self.device, self.args)
            else:
                metrics = self.trainer.test(self.val_global, self.device, self.args)

            test_tot_correct, test_num_sample, test_loss = (
                metrics["test_correct"],
                metrics["test_total"],
                metrics["test_loss"],
            )
            test_tot_corrects.append(copy.deepcopy(test_tot_correct))
            test_num_samples.append(copy.deepcopy(test_num_sample))
            test_losses.append(copy.deepcopy(test_loss))

            # test on test dataset
            test_acc = sum(test_tot_corrects) / sum(test_num_samples)
            test_loss = sum(test_losses) / sum(test_num_samples)
            if self.args.enable_wandb:
                wandb.log({"Test/Acc": test_acc, "round": round_idx})
                wandb.log({"Test/Loss": test_loss, "round": round_idx})
            stats = {"test_acc": test_acc, "test_loss": test_loss}
            logging.info(stats)
