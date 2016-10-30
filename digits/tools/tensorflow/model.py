# Copyright (c) 2016, NVIDIA CORPORATION.  All rights reserved.
#
# This document should comply with PEP-8 Style Guide
# Linter: pylint

"""
Interface for setting up and creating a model in Tensorflow.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import logging
import tensorflow as tf
from tensorflow.python.framework import ops

# Local imports
import tf_data
import utils as digits
from utils import model_property

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s',datefmt='%Y-%m-%d %H:%M:%S',
                    level=logging.INFO)

# Constants
SUMMARIZE_TOWER_STATS = False

# -- from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/models/image/cifar10/cifar10_multi_gpu_train.py
def average_gradients(tower_grads):
    """Calculate the average gradient for each shared variable across all towers.
    Note that this function provides a synchronization point across all towers.
    Args:
      tower_grads: List of lists of (gradient, variable) tuples. The outer list
        is over individual gradients. The inner list is over the gradient
        calculation for each tower.
    Returns:
       List of pairs of (gradient, variable) where the gradient has been averaged
       across all towers.
    """
    with tf.name_scope('gradient_average'):
        average_grads = []
        for grad_and_vars in zip(*tower_grads):
            # Note that each grad_and_vars looks like the following:
            #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
            grads = []
            for g, _ in grad_and_vars:
                # Add 0 dimension to the gradients to represent the tower.
                expanded_g = tf.expand_dims(g, 0)
                # Append on a 'tower' dimension which we will average over below.
                grads.append(expanded_g)
            # Average over the 'tower' dimension.
            grad = tf.concat(0, grads)
            grad = tf.reduce_mean(grad, 0)
            # Keep in mind that the Variables are redundant because they are shared
            # across towers. So .. we will just return the first tower's pointer to
            # the Variable.
            v = grad_and_vars[0][1]
            grad_and_var = (grad, v)
            average_grads.append(grad_and_var)
        return average_grads

class Model(object):
    """
    @TODO(tzaman)

    """
    def __init__(self, stage, croplen, nclasses, optimization=None, momentum=None):
        self.stage = stage
        self.croplen = croplen
        self.nclasses = nclasses
        self.dataloader = None
        self.queue_coord = None
        self.queue_threads = None

        self._optimization = optimization
        self._momentum = momentum
        self.summaries = []
        self.towers = []
        self._train = None

        # Touch to initialize
        if optimization:
            self.learning_rate
            self.global_step
            self.optimizer

    def create_dataloader(self, db_path):
        self.dataloader = tf_data.LoaderFactory.set_source(db_path)
        # @TODO(tzaman) communicate the dataloader summaries to our Model summary list
        self.dataloader.stage = self.stage
        self.dataloader.croplen = self.croplen
        self.dataloader.nclasses = self.nclasses

    def init_dataloader(self):
        with tf.device('/cpu:0'):
            with tf.name_scope(digits.GraphKeys.LOADER):
                self.dataloader.create_input_pipeline()

    def create_model(self, obj_UserModel, stage_scope):

        self.init_dataloader()

        available_devices = digits.get_available_gpus()
        if not available_devices:
            available_devices.append('/cpu:0')

        #available_devices = ['/gpu:0', '/gpu:1'] # DEVELOPMENT : virtual multi-gpu

        # Split the batch over the batch dimension over the number of available gpu's
        if len(available_devices) == 1:
            batch_x_split = [self.dataloader.batch_x]
            if self.stage != digits.STAGE_INF: # Has no labels
                batch_y_split = [self.dataloader.batch_y]
        else:
            with tf.name_scope('parallelize'):
                # Split them up
                batch_x_split = tf.split(0, len(available_devices), self.dataloader.batch_x, name='split_batch')
                if self.stage != digits.STAGE_INF: # Has no labels
                    batch_y_split = tf.split(0, len(available_devices), self.dataloader.batch_y, name='split_batch')

        # Run the user model through the build_model function that should be filled in
        grad_towers = []
        for dev_i, dev_name in enumerate(available_devices):
            with tf.device(dev_name):
                current_scope = stage_scope if len(available_devices) == 1 else ('tower_%d' % dev_i)
                with tf.name_scope(current_scope) as scope_tower:

                    tower_model = self.add_tower(obj_tower=obj_UserModel, x=batch_x_split[dev_i], y=batch_y_split[dev_i])

                    with tf.variable_scope(digits.GraphKeys.MODEL, reuse=dev_i > 0):
                        tower_model.inference # touch to initialize
                
                    if self.stage == digits.STAGE_INF:
                        # For inferencing we will only use the inference part of the graph
                        continue;

                    with tf.name_scope(digits.GraphKeys.LOSS):
                        tf.add_to_collection(digits.GraphKeys.LOSSES, tower_model.loss)

                        # Assemble all made within this scope so far. The user can add custom losses to the digits.GraphKeys.LOSSES collection
                        losses = tf.get_collection(digits.GraphKeys.LOSSES, scope=scope_tower)
                        losses += ops.get_collection(ops.GraphKeys.REGULARIZATION_LOSSES, scope=None)
                        tower_loss = tf.add_n(losses, name='loss')

                        self.summaries.append(tf.scalar_summary(tower_loss.op.name, tower_loss))

                    # Reuse the variables in this scope for the next tower/device
                    tf.get_variable_scope().reuse_variables()

                    if self.stage == digits.STAGE_TRAIN:
                        grad_tower = self.optimizer.compute_gradients(tower_loss)
                        grad_towers.append(grad_tower)

        # Assemble and average the gradients from all towers
        if self.stage == digits.STAGE_TRAIN:
            if len(grad_towers) == 1:
                grad_avg = grad_towers[0]
            else:
                with tf.device(available_devices[0]):
                    grad_avg = average_gradients(grad_towers)
            apply_gradient_op = self.optimizer.apply_gradients(grad_avg, global_step=self.global_step)
            self._train = apply_gradient_op

    def start_queue_runners(self, sess):
        logging.info('Starting queue runners (%s)', self.stage)
        # Distinguish the queue runner collection (for easily obtaining them by collection key)
        queue_runners = tf.get_collection(tf.GraphKeys.QUEUE_RUNNERS, scope=self.stage+'.*')
        for qr in queue_runners:
            if self.stage in qr.name:
                tf.add_to_collection(digits.GraphKeys.QUEUE_RUNNERS, qr)

        self.queue_coord = tf.train.Coordinator()
        self.queue_threads = tf.train.start_queue_runners(sess=sess, coord=self.queue_coord,
                                                          collection=digits.GraphKeys.QUEUE_RUNNERS
                                                         )
        logging.info('Queue runners started (%s)', self.stage)

    def __del__(self):
        # Destructor
        if self.queue_coord:
            # Close and terminate the queues
            self.queue_coord.request_stop()
            self.queue_coord.join(self.queue_threads)

    def add_tower(self, obj_tower, x, y):
        is_training = self.stage == digits.STAGE_TRAIN
        input_shape = self.dataloader.get_shape()
        tower = obj_tower(x, y, input_shape, self.nclasses, is_training)
        self.towers.append(tower)
        return tower

    @model_property
    def train(self):
        return self._train

    @model_property
    def summary(self):
        """
        Merge train summaries
        """
        for t in self.towers:
            self.summaries += t.summaries

        if not len(self.summaries):
            logging.error("No summaries defined. Please define at least one summary.")
            exit(-1)
        return tf.merge_summary(self.summaries)

    @model_property
    def global_step(self):
        # Force global_step on the CPU, becaues the GPU's first step will end at 0 instead of 1.
        with tf.device('/cpu:0'):
            return tf.get_variable('global_step', [], initializer=tf.constant_initializer(0),
                                   trainable=False)

    @model_property
    def learning_rate(self):
        # @TODO(tzaman): the learning rate is a function of the global step, so we could
        #  define it entirely in tf ops, instead of a placeholder and feeding.
        with tf.device('/cpu:0'):
            lr = tf.placeholder(tf.float32, shape=[], name='learning_rate')
            self.summaries.append(tf.scalar_summary('lr', lr))
            return lr

    @model_property
    def optimizer(self):
        logging.info("Optimizer:%s", self._optimization)
        if self._optimization == 'sgd':
            return tf.train.GradientDescentOptimizer(learning_rate=self.learning_rate)
        elif self._optimization == 'adadelta':
            return tf.train.AdadeltaOptimizer(learning_rate=self.learning_rate)
        elif self._optimization == 'adagrad':
            return tf.train.AdagradOptimizer(learning_rate=self.learning_rate)
        elif self._optimization == 'adagradda':
            return tf.train.AdagradDAOptimizer(learning_rate=self.learning_rate,
                                               global_step=self.global_step)
        elif self._optimization == 'momentum':
            return tf.train.MomentumOptimizer(learning_rate=self.learning_rate,
                                              momentum=self._momentum)
        elif self._optimization == 'adam':
            return tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        elif self._optimization == 'ftrl':
            return tf.train.FtrlOptimizer(learning_rate=self.learning_rate)
        elif self._optimization == 'rmsprop':
            return tf.train.RMSPropOptimizer(learning_rate=self.learning_rate,
                                             momentum=self._momentum)
        else:
            logging.error("Invalid optimization flag %s", self._optimization)
            exit(-1)


class Tower(object):

    def __init__(self, x, y, input_shape, nclasses, is_training):
        self.input_shape = input_shape
        self.nclasses = nclasses
        self.is_training = is_training
        self.summaries = []
        self.x = x
        self.y = y
        self.train = None