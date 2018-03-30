import os
import sys
import tensorflow as tf
import numpy as np
from tqdm import tqdm

from wavenet.model import WaveNetModel, create_variable
from apps.vocoder.hparams import hparams

import nnmnkwii.preprocessing as P

class Vocoder(object):
    def __init__(self, max_to_keep=5):
        dilations_factor = hparams.layers // hparams.stacks
        dilations = [2 ** i for j in range(hparams.stacks) for i in range(dilations_factor)]

        self.upsample_factor = hparams.upsample_factor
        global_condition_channels = None
        global_condition_cardinality = None
        if hparams.gc_enable:
            global_condition_channels = hparams.global_channel
            global_condition_cardinality = hparams.global_cardinality

        scalar_input = hparams.input_type == "raw"
        quantization_channels = hparams.quantize_channels[hparams.input_type]
        if scalar_input:
            quantization_channels = None

        with tf.variable_scope('vocoder'):
            self.net = WaveNetModel(batch_size=hparams.batch_size,
                                    dilations=dilations,
                                    filter_width=hparams.filter_width,
                                    scalar_input=scalar_input,
                                    initial_filter_width=hparams.initial_filter_width,
                                    residual_channels=hparams.residual_channels,
                                    dilation_channels=hparams.dilation_channels,
                                    quantization_channels=quantization_channels,
                                    out_channels=hparams.out_channels,
                                    skip_channels=hparams.skip_channels,
                                    global_condition_channels=global_condition_channels,
                                    global_condition_cardinality=global_condition_cardinality,
                                    use_biases=True,
                                    local_condition_dim=hparams.num_mels)

            if hparams.upsample_conditional_features:
                with tf.variable_scope('upsample_layer') as upsample_scope:
                    layer = dict()
                    for i in range(len(hparams.upsample_factor)):
                        layer['upsample{}'.format(i)] = \
                            create_variable('upsample{}'.format(i),
                                            [hparams.upsample_factor[i], self.net.filter_width, 1, 1])
                    self.upsample_var = layer
                    self.upsample_scope = upsample_scope

        self.saver = tf.train.Saver(var_list=tf.trainable_variables(), max_to_keep=max_to_keep)

    def create_upsample(self, l):
        layer_filter = self.upsample_var
        local_condition_batch = tf.expand_dims(l, [3])

        # local condition batch N H W C
        batch_size = tf.shape(local_condition_batch)[0]
        upsample_dim = tf.shape(local_condition_batch)[1]

        for i in range(len(self.upsample_factor)):
            upsample_dim = upsample_dim * self.upsample_factor[i]
            output_shape = tf.stack([batch_size, upsample_dim, tf.shape(local_condition_batch)[2], 1])
            local_condition_batch = tf.nn.conv2d_transpose(
                local_condition_batch,
                layer_filter['upsample{}'.format(i)],
                strides=[1, self.upsample_factor[i], 1, 1],
                output_shape=output_shape
            )

        local_condition_batch = tf.squeeze(local_condition_batch, [3])
        return local_condition_batch

    def loss(self, x, l, g):
        l = self.create_upsample(l)
        loss = self.net.loss(x, l, g, l2_regularization_strength=hparams.l2_regularization_strength)
        self.upsampled_lc = l

        return loss

    def save(self, sess, logdir, step):
        model_name = 'model.ckpt'
        checkpoint_path = os.path.join(logdir, model_name)
        print('Storing checkpoint to {} ...'.format(logdir), end="")
        sys.stdout.flush()

        if not os.path.exists(logdir):
            os.makedirs(logdir)

        self.saver.save(sess, checkpoint_path, global_step=step)
        print(' Done.')

    def load(self, sess, logdir):
        print("Trying to restore saved checkpoints from {} ...".format(logdir),
              end="")

        ckpt = tf.train.get_checkpoint_state(logdir)
        if ckpt:
            print("  Checkpoint found: {}".format(ckpt.model_checkpoint_path))
            global_step = int(ckpt.model_checkpoint_path
                              .split('/')[-1]
                              .split('-')[-1])
            print("  Global step was: {}".format(global_step))
            print("  Restoring...", end="")
            self.saver.restore(sess, ckpt.model_checkpoint_path)
            print(" Done.")
            return global_step, sess
        else:
            print(" No checkpoint found.")
            return None, sess


class Generator(object):
    def __init__(self, vocoder, gc_enable=True, batch_size=1):
        # For generation
        self.vocoder = vocoder
        self.batch_size = batch_size
        if vocoder.net.scalar_input:
            self.sample_placeholder = tf.placeholder(tf.float32)
        else:
            self.sample_placeholder = tf.placeholder(tf.int32)

        self.lc_placeholder = tf.placeholder(tf.float32)
        self.gc_placeholder = tf.placeholder(tf.int32) if gc_enable else None

        self.gen_num = tf.placeholder(tf.int32)

        self.next_sample_prob, self.layers_out, self.qs = \
            vocoder.net.predict_proba_incremental(self.sample_placeholder,
                                                  self.gen_num,
                                                  batch_size=batch_size,
                                                  local_condition=self.lc_placeholder,
                                                  global_condition=self.gc_placeholder
                                                  )
        self.initial = tf.placeholder(tf.float32)
        self.others = tf.placeholder(tf.float32)
        self.update_q_ops = \
            vocoder.net.create_update_q_ops(self.qs,
                                            self.initial,
                                            self.others,
                                            self.gen_num,
                                            batch_size=batch_size)

        self.var_q = vocoder.net.get_vars_q()

    def load(self, sess, log_dir):
        self.vocoder.load(sess, log_dir)

    def generate(self, sess, n_samples, lc, gc):
        sess.run(tf.variables_initializer(self.var_q))
        receptive_field = self.vocoder.net.receptive_field

        if self.vocoder.net.scalar_input:
            seeds = [0]
        else:
            seeds = [128]

        seeds = [seeds]
        seeds = np.repeat(seeds, self.batch_size, axis=0)
        generated = []

        for j in tqdm(range(receptive_field + n_samples)):
            if j < receptive_field:
                sample = seeds
                current_lc = np.zeros((self.batch_size, hparams.num_mels))
            else:
                sample = generated[-1]
                current_lc = lc[:, j - receptive_field, :]

            # Generation phase
            feed_dict = {
                self.sample_placeholder: sample,
                self.lc_placeholder: current_lc,
                self.gen_num: j}

            if self.gc_placeholder is not None:
                feed_dict.update({self.gc_placeholder: gc})

            prob, _layers = sess.run([self.next_sample_prob, self.layers_out], feed_dict=feed_dict)

            # Update phase
            feed_dict = {
                self.initial: _layers[0],
                self.others: np.array(_layers[1:]),
                self.gen_num: j}

            sess.run(self.update_q_ops, feed_dict=feed_dict)

            if self.vocoder.net.scalar_input:
                generated_sample = prob
            else:
                # TODO: random choice
                generated_sample = np.argmax(prob, axis=-1)

            generated.append(generated_sample)

        result = np.hstack(generated)[:, receptive_field:]
        if not self.vocoder.net.scalar_input:
            result = P.inv_mulaw_quantize(result.astype(np.int16), self.vocoder.net.quantization_channels)

        return result