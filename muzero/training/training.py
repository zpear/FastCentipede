"""Training module: this is where MuZero neurons are trained."""
import numpy as np
from display import progress_bar
import tensorflow_core as tf
from tensorflow_core.python.keras.losses import MSE
from copy import copy

from config import MuZeroConfig
from networks.network import BaseNetwork
from networks.shared_storage import SharedStorage
from training.replay_buffer import ReplayBuffer


def train_network(config: MuZeroConfig, storage: SharedStorage, replay_buffer: ReplayBuffer, epochs: int):
    network = storage.current_network
    optimizer = storage.optimizer

    for _ in range(epochs):
        progress_bar(_, epochs, name='Training')
        batch = replay_buffer.sample_batch(config.num_unroll_steps, config.td_steps)
        update_weights(optimizer, network, batch)
        storage.save_network(network.training_steps, network)
    if epochs:
        progress_bar(epochs, epochs, name='Training')
        print('{}'.format(' '*60), end='\r')
    storage.save_network_to_disk(network)


def update_weights(optimizer: tf.keras.optimizers, network: BaseNetwork, batch):
    def scale_gradient(tensor, scale: float):
        """Trick function to scale the gradient in tensorflow"""
        return (1. - scale) * tf.stop_gradient(tensor) + scale * tensor

    def loss():
        loss = 0
        image_batch, targets_init_batch, targets_time_batch, actions_time_batch, mask_time_batch, dynamic_mask_time_batch = batch

        # Initial step, from the real observation: representation + prediction networks
        representation_batch, value_batch, policy_batch = network.initial_model(np.array(image_batch))

        # Only update the element with a policy target
        target_value_batch, _, target_policy_batch = zip(*targets_init_batch)
        mask_policy = list(map(lambda l: bool(l), target_policy_batch))
        target_policy_batch = list(filter(lambda l: bool(l), target_policy_batch))
        policy_batch = tf.boolean_mask(policy_batch, mask_policy)

        # Compute the loss of the first pass
        loss += tf.math.reduce_mean(loss_value(target_value_batch, value_batch, network.value_support_size))
        losses = [copy(loss)]
        loss += tf.math.reduce_mean(
            tf.nn.softmax_cross_entropy_with_logits(logits=policy_batch, labels=target_policy_batch))
        losses.append(loss)
        # Recurrent steps, from action and previous hidden state.
        for actions_batch, targets_batch, mask, dynamic_mask in zip(actions_time_batch, targets_time_batch,
                                                                    mask_time_batch, dynamic_mask_time_batch):
            target_value_batch, target_reward_batch, target_policy_batch = zip(*targets_batch)

            # Only execute BPTT for elements with an action
            representation_batch = tf.boolean_mask(representation_batch, dynamic_mask)
            target_value_batch = tf.boolean_mask(target_value_batch, mask)
            target_reward_batch = tf.boolean_mask(target_reward_batch, mask)

            # Creating conditioned_representation: concatenate representations with actions batch
            actions_batch = tf.one_hot(actions_batch, network.action_size)

            # TODO: make this reshape dynamic
            actions_batch = tf.reshape(actions_batch, (actions_batch.shape[0], 3, 3, 1))

            paddings = tf.constant([[0, 0],
                                    [0, max(0, representation_batch.shape[1] - actions_batch.shape[1])],
                                    [0, max(0, representation_batch.shape[2] - actions_batch.shape[2])],
                                    [0, 0]])
            actions_batch = tf.pad(actions_batch, paddings, "CONSTANT")

            # Recurrent step from conditioned representation: recurrent + prediction networks
            conditioned_representation_batch = tf.concat((representation_batch, actions_batch), axis=3)
            representation_batch, reward_batch, value_batch, policy_batch = network.recurrent_model(
                conditioned_representation_batch)

            # Only execute BPTT for elements with a policy target
            target_policy_batch = [policy for policy, b in zip(target_policy_batch, mask) if b]
            mask_policy = list(map(lambda l: bool(l), target_policy_batch))
            target_policy_batch = tf.convert_to_tensor([policy for policy in target_policy_batch if policy])
            policy_batch = tf.boolean_mask(policy_batch, mask_policy)

            # Compute the partial loss
            l = (tf.math.reduce_mean(loss_value(target_value_batch, value_batch, network.value_support_size)) +
                 MSE(target_reward_batch, tf.squeeze(reward_batch)) +
                 tf.math.reduce_mean(
                     tf.nn.softmax_cross_entropy_with_logits(logits=policy_batch, labels=target_policy_batch)))

            # Scale the gradient of the loss by the average number of actions unrolled
            gradient_scale = 1. / len(actions_time_batch)
            #print('contribution', scale_gradient(l, gradient_scale))
            #print(l)

            grad = scale_gradient(l, gradient_scale)
            loss += grad
            losses.append(grad)

            # Half the gradient of the representation
            representation_batch = scale_gradient(representation_batch, 0.5)

        # Cap by threshold to prevent exploding gradient (https://arxiv.org/pdf/1211.5063.pdf)
        threshold = 9.9999999e25
        loss = loss * (threshold/max(loss, threshold))
        print('\t loss: {}'.format(loss), end='\r')

        if tf.math.is_nan(loss):
            print("WARNING: VANISHED GRADIENT")
            print(losses)
        return loss

    optimizer.minimize(loss=loss, var_list=network.cb_get_variables())
    network.training_steps += 1


def loss_value(target_value_batch, value_batch, value_support_size: int):
    batch_size = len(target_value_batch)
    targets = np.zeros((batch_size, value_support_size))
    sqrt_value = np.sqrt(target_value_batch) # + abs(np.amin(target_value_batch)))
    # qrt of negative = floor of nan = big negative
    floor_value = np.floor(sqrt_value).astype(int)
    #print(floor_value)

    #import pdb
    #pdb.set_trace()
    floor_value = np.clip(floor_value, a_min=0, a_max=value_support_size-2)
    rest = sqrt_value - floor_value
    targets[range(batch_size), floor_value.astype(int)] = 1 - rest
    targets[range(batch_size), floor_value.astype(int) + 1] = rest

    val = tf.nn.softmax_cross_entropy_with_logits(logits=value_batch, labels=targets)
    #tf.math.reduce_mean(loss)
    if tf.math.is_nan(tf.math.reduce_mean(val)):
        import pdb
        pdb.set_trace()
    return val
