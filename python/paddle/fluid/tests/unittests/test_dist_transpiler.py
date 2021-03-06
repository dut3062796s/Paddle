#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import math

import unittest
import paddle.fluid as fluid
from paddle.fluid.transpiler.distribute_transpiler import delete_ops
import traceback
import collections
import six


class TranspilerTest(unittest.TestCase):
    def setUp(self):
        self.trainer_id = 0
        self.trainers = 2
        self.pservers = 2
        # NOTE: we do not actually bind this port
        self.pserver_eps = "127.0.0.1:6174,127.0.0.1:6175"
        self.pserver1_ep = "127.0.0.1:6174"
        self.pserver2_ep = "127.0.0.1:6175"
        self.sync_mode = True
        self.transpiler = None

    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        sgd_optimizer = fluid.optimizer.SGD(learning_rate=0.1)
        sgd_optimizer.minimize(avg_cost)

    def get_main_program(self):
        main = fluid.Program()
        main.random_seed = 1
        with fluid.program_guard(main):
            self.net_conf()
        self.origin_prog = main.clone()
        return main

    def get_trainer(self, config=None):
        src = fluid.default_startup_program().clone()

        t = self._transpiler_instance(config)

        trainer_main = t.get_trainer_program()
        trainer_startup = fluid.default_startup_program()

        assert (src.num_blocks == 1)
        assert (trainer_startup.num_blocks == src.num_blocks)

        return trainer_main, trainer_startup

    def get_pserver(self, ep, config=None, sync_mode=True):
        t = self._transpiler_instance(config, sync_mode)
        pserver = t.get_pserver_program(ep)
        startup = t.get_startup_program(ep, pserver)
        return pserver, startup

    def _transpiler_instance(self, config=None, sync_mode=True):
        if not self.transpiler:
            main = self.get_main_program()
            self.transpiler = fluid.DistributeTranspiler(config=config)
            self.transpiler.transpile(
                self.trainer_id,
                program=main,
                pservers=self.pserver_eps,
                trainers=self.trainers,
                sync_mode=sync_mode)

        return self.transpiler

    def transpiler_test_impl(self):
        pass

    def test_transpiler(self):
        main = fluid.Program()
        startup = fluid.Program()
        with fluid.unique_name.guard():
            with fluid.program_guard(main, startup):
                self.transpiler_test_impl()


class TestBasicModel(TranspilerTest):
    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        pserver2, startup2 = self.get_pserver(self.pserver2_ep)

        trainer, trainer_startup = self.get_trainer()

        # splited var blocks should be in startup program
        self.assertTrue("fc_w.block0" in trainer_startup.global_block().vars)
        self.assertTrue("fc_w.block1" in trainer_startup.global_block().vars)
        self.assertTrue("fc_w" in trainer_startup.global_block().vars)
        self.assertTrue("fc_b" in trainer_startup.global_block().vars)
        self.assertTrue("fc_w@GRAD" not in trainer_startup.global_block().vars)
        self.assertTrue("fc_b@GRAD" not in trainer_startup.global_block().vars)

        src = [op.type for op in trainer_startup.global_block().ops]
        dst = ['fill_constant', 'fill_constant', 'uniform_random', 'recv', 'recv', \
               'fetch_barrier', 'concat']

        self.assertEqual(src, dst)

        self.assertEqual([op.type for op in trainer.global_block().ops], [
            'mul', 'elementwise_add', 'elementwise_sub', 'square', 'mean',
            'fill_constant', 'mean_grad', 'square_grad', 'elementwise_sub_grad',
            'elementwise_add_grad', 'send', 'mul_grad', 'split_byref', 'send',
            'send_barrier', 'recv', 'recv', 'fetch_barrier', 'concat'
        ])

        self.assertEqual(len(pserver.blocks), 3)
        # block0: listen_and_serv
        self.assertEqual([op.type for op in pserver.blocks[0].ops],
                         ["listen_and_serv"])
        # block1~2: optimize pass
        self.assertEqual([op.type for op in pserver.blocks[1].ops],
                         ["sum", "scale", "sgd"])
        # confirm startup program
        self.assertEqual([op.type for op in startup.global_block().ops],
                         ["fill_constant", "fill_constant", "uniform_random"])
        # the variable #fc_w will be split into two blocks
        fc_w_var = startup.global_block().var("fc_w.block1")
        self.assertEqual(fc_w_var.shape, (500, 1000))
        # all parameters should be optimized on pserver

        pserver_params = []
        for prog in [pserver, pserver2]:
            for blk in prog.blocks:
                for op in blk.ops:
                    if "Param" in op.input_names:
                        param_name = op.input("Param")[0]
                        is_block_idx = param_name.find(".block")
                        if is_block_idx != -1:
                            origin_param_name = param_name[:is_block_idx]
                        else:
                            origin_param_name = param_name
                        pserver_params.append(origin_param_name)
        trainer_params = []
        for op in self.origin_prog.global_block().ops:
            if "Param" in op.input_names:
                trainer_params.append(op.input("Param")[0])
        self.assertEqual(set(pserver_params), set(trainer_params))


class TestBasicModelWithLargeBlockSize(TranspilerTest):
    def transpiler_test_impl(self):
        config = fluid.DistributeTranspilerConfig()
        config.min_block_size = 1048576

        pserver, startup = self.get_pserver(self.pserver1_ep, config)
        pserver2, startup2 = self.get_pserver(self.pserver2_ep, config)

        trainer, _ = self.get_trainer(config)

        self.assertEqual([op.type for op in trainer.global_block().ops], [
            'mul', 'elementwise_add', 'elementwise_sub', 'square', 'mean',
            'fill_constant', 'mean_grad', 'square_grad', 'elementwise_sub_grad',
            'elementwise_add_grad', 'send', 'mul_grad', 'send', 'send_barrier',
            'recv', 'recv', 'fetch_barrier'
        ])

        self.assertEqual(len(pserver.blocks), 2)
        # block0: listen_and_serv
        self.assertEqual([op.type for op in pserver.blocks[0].ops],
                         ["listen_and_serv"])
        # block1~2: optimize pass
        self.assertEqual([op.type for op in pserver.blocks[1].ops],
                         ["sum", "scale", "sgd"])
        # confirm startup program
        self.assertEqual([op.type for op in startup.global_block().ops],
                         ["fill_constant", "fill_constant"])
        # the variable #fc_w will be split into two blocks
        fc_w_var = startup2.global_block().var("fc_w")
        self.assertEqual(fc_w_var.shape, (1000, 1000))
        # all parameters should be optimized on pserver

        pserver_params = []
        for prog in [pserver, pserver2]:
            for blk in prog.blocks:
                for op in blk.ops:
                    if "Param" in op.input_names:
                        param_name = op.input("Param")[0]
                        is_block_idx = param_name.find(".block")
                        if is_block_idx != -1:
                            origin_param_name = param_name[:is_block_idx]
                        else:
                            origin_param_name = param_name
                        pserver_params.append(origin_param_name)
        trainer_params = []
        for op in self.origin_prog.global_block().ops:
            if "Param" in op.input_names:
                trainer_params.append(op.input("Param")[0])
        self.assertEqual(set(pserver_params), set(trainer_params))


class TestNoSliceVar(TranspilerTest):
    def setUp(self):
        super(TestNoSliceVar, self).setUp()

    def transpiler_test_impl(self):
        config = fluid.DistributeTranspilerConfig()
        config.slice_var_up = False

        _, startup = self.get_pserver(self.pserver1_ep, config)
        _, startup2 = self.get_pserver(self.pserver2_ep, config)

        if "fc_w" in startup.global_block().vars:
            fc_w_var = startup.global_block().vars["fc_w"]
        elif "fc_w" in startup2.global_block().vars:
            fc_w_var = startup2.global_block().vars["fc_w"]

        self.assertEqual(fc_w_var.shape, (1000, 1000))


class TestLRDecay(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        sgd_optimizer = fluid.optimizer.SGD(
            learning_rate=fluid.layers.exponential_decay(
                learning_rate=1.0,
                decay_steps=2100,
                decay_rate=0.1,
                staircase=True))
        sgd_optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        trainer, _ = self.get_trainer()

        self.assertEqual(len(pserver.blocks), 4)
        lr_decay_ops = [op.type for op in pserver.blocks[1].ops]
        self.assertEqual(lr_decay_ops, [
            "increment", "cast", "fill_constant", "elementwise_div", "floor",
            "fill_constant", "elementwise_pow", "fill_constant",
            "elementwise_mul"
        ])


class TestLRDecayConditional(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        sgd_optimizer = fluid.optimizer.SGD(
            learning_rate=fluid.layers.piecewise_decay([10000, 20000],
                                                       [1.0, 0.5, 1.0]))
        sgd_optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        trainer, _ = self.get_trainer()

        serv_op = pserver.blocks[0].ops[0]
        sub_blocks = []
        optimize_blocks = []
        for b in serv_op.all_attrs()["optimize_blocks"]:
            optimize_blocks.append(b.idx)
        for b in pserver.blocks:
            if b.idx not in optimize_blocks:
                sub_blocks.append(b.idx)

        self.assertEqual(len(pserver.blocks), 7)
        lr_decay_ops = [op.type for op in pserver.blocks[1].ops]
        self.assertEqual(lr_decay_ops, [
            "increment", "cast", "fill_constant", "fill_constant", "less_than",
            "logical_not", "conditional_block", "fill_constant",
            "fill_constant", "less_than", "logical_not", "logical_and",
            "logical_and", "conditional_block", "fill_constant",
            "conditional_block"
        ])
        # test the condition blocks
        for b in sub_blocks:
            if b == 0:
                continue
            block = pserver.blocks[b]
            self.assertEqual([op.type for op in block.ops], ["assign"])


class TestL2Decay(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(
            input=x,
            size=1000,
            act=None,
            param_attr=fluid.ParamAttr(
                name='fc_w',
                regularizer=fluid.regularizer.L2Decay(),
                gradient_clip=fluid.clip.GradientClipByValue(0.1)),
            bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        sgd_optimizer = fluid.optimizer.SGD(learning_rate=0.1)
        sgd_optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        trainer, _ = self.get_trainer()

        self.assertEqual(len(pserver.blocks), 3)
        self.assertEqual([op.type for op in pserver.blocks[1].ops],
                         ["sum", "scale", "clip", "sgd"])
        self.assertEqual(
            [op.type for op in pserver.blocks[2].ops],
            ["sum", "scale", "clip", "scale", "elementwise_add", "sgd"])
        # TODO(typhoonzero): test clipping and L2Decay ops are removed from trainer


class TestL2DecayWithPiecewise(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        base_lr = 1.0
        bd = [1, 10, 20, 30]
        lr = [base_lr * (0.1**i) for i in range(len(bd) + 1)]
        sgd_optimizer = fluid.optimizer.Momentum(
            learning_rate=fluid.layers.piecewise_decay(
                boundaries=bd, values=lr),
            momentum=0.9,
            regularization=fluid.regularizer.L2Decay(1e-4))
        sgd_optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        trainer, _ = self.get_trainer()

        self.assertEqual(len(pserver.blocks), 9)
        self.assertEqual([op.type for op in pserver.blocks[1].ops], [
            "increment", "cast", "fill_constant", "fill_constant", "less_than",
            "logical_not", "conditional_block", "fill_constant",
            "fill_constant", "less_than", "logical_not", "logical_and",
            "logical_and", "conditional_block", "fill_constant",
            "fill_constant", "less_than", "logical_not", "logical_and",
            "logical_and", "conditional_block", "fill_constant",
            "fill_constant", "less_than", "logical_not", "logical_and",
            "logical_and", "conditional_block", "fill_constant",
            "conditional_block"
        ])
        self.assertEqual(
            [op.type for op in pserver.blocks[7].ops],
            ["sum", "scale", "scale", "elementwise_add", "momentum"])
        self.assertEqual(
            [op.type for op in pserver.blocks[8].ops],
            ["sum", "scale", "scale", "elementwise_add", "momentum"])


class TestDistLookupTableBase(TranspilerTest):
    def network_with_table(self, is_sparse, is_distributed):
        self.table_size = 1000
        self.emb_size = 64
        self.lookup_table_name = 'shared_w'

        def emb_pool(ids):
            emb = fluid.layers.embedding(
                input=ids,
                size=[self.table_size, self.emb_size],
                dtype='float32',
                param_attr=self.lookup_table_name,  # share parameter
                is_sparse=is_sparse,
                is_distributed=is_distributed)
            pool = fluid.layers.sequence_pool(input=emb, pool_type='average')
            return pool

        title_ids = fluid.layers.data(
            name='title_ids', shape=[1], dtype='int64', lod_level=1)
        brand_ids = fluid.layers.data(
            name='brand_ids', shape=[1], dtype='int64', lod_level=1)
        title_emb = emb_pool(title_ids)
        brand_emb = emb_pool(brand_ids)
        fc0 = fluid.layers.concat(input=[title_emb, brand_emb], axis=1)
        predict = fluid.layers.fc(input=fc0,
                                  size=2,
                                  act=None,
                                  param_attr=fluid.ParamAttr(name='fc_w'),
                                  bias_attr=fluid.ParamAttr(name='fc_b'))

        label = fluid.layers.data(name='label', shape=[1], dtype='int64')
        cost = fluid.layers.cross_entropy(input=predict, label=label)
        avg_cost = fluid.layers.mean(cost)
        optimizer = fluid.optimizer.Adam(learning_rate=0.003)
        optimizer.minimize(avg_cost)


class TestLocalLookupTable(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=False)

    def transpiler_test_impl(self):
        pserver1, startup1 = self.get_pserver(self.pserver1_ep)

        self.assertEqual(len(pserver1.blocks), 3)
        # 0 listen_and_serv
        # 1 optimize for fc_w or fc_b adam
        self.assertEqual([op.type for op in pserver1.blocks[1].ops],
                         ["sum", "scale", "adam", "scale", "scale"])
        # 2 optimize for table adam
        # NOTE: if param is not selected rows, the grad will scaled to grad / trainer_num
        self.assertEqual([op.type for op in pserver1.blocks[2].ops],
                         ["sum", "scale", "adam", "scale", "scale"])

        trainer, _ = self.get_trainer()
        self.assertEqual(len(trainer.blocks), 1)
        ops = [
            'lookup_table', 'sequence_pool', 'lookup_table', 'sequence_pool',
            'concat', 'mul', 'elementwise_add', 'cross_entropy', 'mean',
            'fill_constant', 'mean_grad', 'cross_entropy_grad',
            'elementwise_add_grad', 'send', 'mul_grad', 'send', 'concat_grad',
            'sequence_pool_grad', 'lookup_table_grad', 'sequence_pool_grad',
            'lookup_table_grad', 'sum', 'split_selected_rows', 'send',
            'send_barrier', 'recv', 'recv', 'recv', 'fetch_barrier', 'concat'
        ]
        self.assertEqual([op.type for op in trainer.blocks[0].ops], ops)


class TestDistLookupTable(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=True)

    def transpiler_test_impl(self):
        pserver1, startup1 = self.get_pserver(self.pserver1_ep)

        self.assertEqual(len(pserver1.blocks), 6)
        # 0 listen_and_serv
        # 1 optimize for fc_w or fc_b adam
        self.assertEqual([op.type for op in pserver1.blocks[1].ops],
                         ["sum", "scale", "adam", "scale", "scale"])
        # 2 optimize for table sgd
        self.assertEqual([op.type for op in pserver1.blocks[2].ops],
                         ["sum", "sgd"])
        # 3 prefetch -> lookup_sparse_table for data0
        self.assertEqual([op.type for op in pserver1.blocks[3].ops],
                         ["lookup_sparse_table"])
        # 4 prefetch -> lookup_sparse_table for data1
        self.assertEqual([op.type for op in pserver1.blocks[4].ops],
                         ["lookup_sparse_table"])
        # 5 save table
        self.assertEqual([op.type for op in pserver1.blocks[5].ops], ["save"])

        trainer, _ = self.get_trainer()
        self.assertEqual(len(trainer.blocks), 1)
        ops = [
            'split_ids', 'prefetch', 'merge_ids', 'sequence_pool', 'split_ids',
            'prefetch', 'merge_ids', 'sequence_pool', 'concat', 'mul',
            'elementwise_add', 'cross_entropy', 'mean', 'fill_constant',
            'mean_grad', 'cross_entropy_grad', 'elementwise_add_grad', 'send',
            'mul_grad', 'send', 'concat_grad', 'sequence_pool_grad',
            'lookup_table_grad', 'sequence_pool_grad', 'lookup_table_grad',
            'sum', 'split_ids', 'send', 'send_barrier', 'recv', 'recv',
            'fetch_barrier'
        ]
        self.assertEqual([op.type for op in trainer.blocks[0].ops], ops)


class TestAsyncLocalLookupTable(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=False)

    def transpiler_test_impl(self):
        config = fluid.DistributeTranspilerConfig()
        pserver1, startup1 = self.get_pserver(self.pserver1_ep, config, False)

        self.assertEqual(len(pserver1.blocks), 3)
        # 0 listen_and_serv
        # 1 optimize for fc_w or fc_b adam
        self.assertEqual([op.type for op in pserver1.blocks[1].ops],
                         ["adam", "scale", "scale"])
        # 2 optimize for table adam
        # NOTE: if param is not selected rows, the grad will scaled to grad / trainer_num
        self.assertEqual([op.type for op in pserver1.blocks[2].ops],
                         ["adam", "scale", "scale"])

        trainer, _ = self.get_trainer(config)
        self.assertEqual(len(trainer.blocks), 1)
        ops = [
            'lookup_table', 'sequence_pool', 'lookup_table', 'sequence_pool',
            'concat', 'mul', 'elementwise_add', 'cross_entropy', 'mean',
            'fill_constant', 'mean_grad', 'cross_entropy_grad',
            'elementwise_add_grad', 'send', 'mul_grad', 'send', 'concat_grad',
            'sequence_pool_grad', 'lookup_table_grad', 'sequence_pool_grad',
            'lookup_table_grad', 'sum', 'split_selected_rows', 'send', 'recv',
            'recv', 'recv', 'concat'
        ]
        self.assertEqual([op.type for op in trainer.blocks[0].ops], ops)


class TestAsyncDistLookupTable(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=True)

    def transpiler_test_impl(self):
        config = fluid.DistributeTranspilerConfig()

        pserver1, startup1 = self.get_pserver(self.pserver1_ep, config, False)

        self.assertEqual(len(pserver1.blocks), 6)
        # 0 listen_and_serv
        # 1 optimize for fc_w or fc_b adam
        self.assertEqual([op.type for op in pserver1.blocks[1].ops],
                         ["adam", "scale", "scale"])
        # 2 optimize for table sgd
        self.assertEqual([op.type for op in pserver1.blocks[2].ops], ["sgd"])
        # 3 prefetch -> lookup_sparse_table for data0
        self.assertEqual([op.type for op in pserver1.blocks[3].ops],
                         ["lookup_sparse_table"])
        # 4 prefetch -> lookup_sparse_table for data1
        self.assertEqual([op.type for op in pserver1.blocks[4].ops],
                         ["lookup_sparse_table"])
        # 5 save table
        self.assertEqual([op.type for op in pserver1.blocks[5].ops], ["save"])

        trainer, _ = self.get_trainer(config)
        self.assertEqual(len(trainer.blocks), 1)
        ops = [
            'split_ids', 'prefetch', 'merge_ids', 'sequence_pool', 'split_ids',
            'prefetch', 'merge_ids', 'sequence_pool', 'concat', 'mul',
            'elementwise_add', 'cross_entropy', 'mean', 'fill_constant',
            'mean_grad', 'cross_entropy_grad', 'elementwise_add_grad', 'send',
            'mul_grad', 'send', 'concat_grad', 'sequence_pool_grad',
            'lookup_table_grad', 'sequence_pool_grad', 'lookup_table_grad',
            'sum', 'split_ids', 'send', 'recv', 'recv'
        ]
        self.assertEqual([op.type for op in trainer.blocks[0].ops], ops)


class TestDistLookupTableSliceSize(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=True)

    def transpiler_test_impl(self):
        config = fluid.DistributeTranspilerConfig()
        pserver1, _ = self.get_pserver(self.pserver1_ep, config)

        self.assertTrue(self.transpiler.has_distributed_lookup_table)
        lookup_table_var = pserver1.global_block().vars[
            self.transpiler.table_name]
        row_size = lookup_table_var.shape[0]
        calc_row_size = int(math.ceil(self.table_size / self.pservers))
        self.assertEqual(row_size, calc_row_size)


class TestDistArgsInProgram(TestDistLookupTableBase):
    def net_conf(self):
        self.network_with_table(is_sparse=True, is_distributed=True)

    def transpiler_test_impl(self):
        trainer, _ = self.get_trainer()

        self.assertTrue(trainer._is_distributed)
        self.assertTrue(trainer._is_chief)
        self.assertEqual(trainer._distributed_lookup_table,
                         self.lookup_table_name)
        self.assertEqual(trainer._endpoints,
                         [self.pserver1_ep, self.pserver2_ep])


class TestRMSPropOptimizer(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        optimizer = fluid.optimizer.RMSProp(learning_rate=0.1)
        optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, startup = self.get_pserver(self.pserver1_ep)
        pserver2, startup2 = self.get_pserver(self.pserver2_ep)

        self.assertEqual(len(pserver.blocks), 3)
        # block1~2: optimize pass
        self.assertEqual([op.type for op in pserver.blocks[1].ops],
                         ["sum", "scale", "rmsprop"])
        # the variable #fc_w will be split into two blocks
        fc_w_var = startup.global_block().var("fc_w.block1")
        self.assertEqual(fc_w_var.shape, (500, 1000))
        moment_var = startup.global_block().var("momentum_1")
        self.assertEqual(moment_var.shape, (500, 1000))


class TestLoadSliceVar(TranspilerTest):
    def net_conf(self):
        x = fluid.layers.data(name='x', shape=[1000], dtype='float32')
        y_predict = fluid.layers.fc(input=x,
                                    size=1000,
                                    act=None,
                                    param_attr=fluid.ParamAttr(name='fc_w'),
                                    bias_attr=fluid.ParamAttr(name='fc_b'))
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')
        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        optimizer = fluid.optimizer.RMSProp(learning_rate=0.1)
        optimizer.minimize(avg_cost)

    def transpiler_test_impl(self):
        pserver, _ = self.get_pserver(self.pserver1_ep)
        pserver2, _ = self.get_pserver(self.pserver2_ep)

        self.assertTrue(pserver._slice_vars_and_attrs)
        self.assertTrue(pserver2._slice_vars_and_attrs)

        for idx in six.moves.xrange(len(pserver._slice_vars_and_attrs)):
            self.assertEqual(pserver._slice_vars_and_attrs[idx][0],
                             pserver2._slice_vars_and_attrs[idx][0])

            total_numel = six.moves.reduce(
                lambda x, y: x * y, pserver._slice_vars_and_attrs[idx][0].shape)
            self.assertEqual(
                total_numel,
                six.moves.reduce(lambda x, y: x * y,
                                 pserver._slice_vars_and_attrs[idx][2].shape) +
                six.moves.reduce(lambda x, y: x * y,
                                 pserver2._slice_vars_and_attrs[idx][2].shape))


if __name__ == "__main__":
    unittest.main()
