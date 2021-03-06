import os
import tempfile

import tensorflow as tf
import zipfile
import cloudpickle
import numpy as np

from baselines import logger, deepq
from baselines.common import tf_util
from baselines.common.tf_util import load_state, save_state
from baselines.common.schedules import LinearSchedule
from baselines.deepq.replay_buffer import ReplayBuffer, PrioritizedReplayBuffer
from baselines.deepq.utils import ObservationInput


class ActWrapper(object):
    def __init__(self, act, act_params, sess=None):
        """
        the actor wrapper for loading and saving

        :param act: (function (TensorFlow Tensor, bool, float): TensorFlow Tensor) the actor function
        :param act_params: (dict) {'make_obs_ph', 'q_func', 'num_actions'}
        :param sess: (TensorFlow Session) the current session
        """
        self._act = act
        self._act_params = act_params
        if sess is None:
            self.sess = tf_util.make_session()
        else:
            self.sess = sess

    @staticmethod
    def load(path):
        """
        Load from a path an actor model

        :param path: (str) the save location
        :return: (ActWrapper) a loaded actor model
        """
        with open(path, "rb") as file_handler:
            model_data, act_params = cloudpickle.load(file_handler)
        act = deepq.build_act(**act_params)
        sess = tf_util.make_session()
        with tempfile.TemporaryDirectory() as temp_dir:
            arc_path = os.path.join(temp_dir, "packed.zip")
            with open(arc_path, "wb") as file_handler:
                file_handler.write(model_data)

            zipfile.ZipFile(arc_path, 'r', zipfile.ZIP_DEFLATED).extractall(temp_dir)
            load_state(os.path.join(temp_dir, "model"), sess)

        return ActWrapper(act, act_params, sess=sess)

    def __call__(self, *args, **kwargs):
        with self.sess.as_default():
            return self._act(*args, **kwargs)

    def save(self, path=None):
        """
        Save model to a pickle located at `path`

        :param path: (str) the save location
        """
        if path is None:
            path = os.path.join(logger.get_dir(), "model.pkl")

        with tempfile.TemporaryDirectory() as temp_dir:
            save_state(os.path.join(temp_dir, "model"), self.sess)
            arc_name = os.path.join(temp_dir, "packed.zip")
            with zipfile.ZipFile(arc_name, 'w') as zipf:
                for root, _, files in os.walk(temp_dir):
                    for fname in files:
                        file_path = os.path.join(root, fname)
                        if file_path != arc_name:
                            zipf.write(file_path, os.path.relpath(file_path, temp_dir))
            with open(arc_name, "rb") as file_handler:
                model_data = file_handler.read()
        with open(path, "wb") as file_handler:
            cloudpickle.dump((model_data, self._act_params), file_handler)


def load(path):
    """
    Load act function that was returned by learn function.

    :param path: (str) path to the act function pickle

    :return: (ActWrapper) function that takes a batch of observations and returns actions.
    """
    return ActWrapper.load(path)


def learn(env, q_func, learning_rate=5e-4, max_timesteps=100000, buffer_size=50000, exploration_fraction=0.1,
          exploration_final_eps=0.02, train_freq=1, batch_size=32, print_freq=100, checkpoint_freq=10000,
          checkpoint_path=None, learning_starts=1000, gamma=1.0, target_network_update_freq=500,
          prioritized_replay=False, prioritized_replay_alpha=0.6, prioritized_replay_beta0=0.4,
          prioritized_replay_beta_iters=None, prioritized_replay_eps=1e-6, param_noise=False, callback=None):
    """
    Train a deepq model.

    :param env: (Gym Environment) environment to train on
    :param q_func: (function (TensorFlow Tensor, int, str, bool): TensorFlow Tensor)
        the model that takes the following inputs:
            - observation_in: (object) the output of observation placeholder
            - num_actions: (int) number of actions
            - scope: (str)
            - reuse: (bool) should be passed to outer variable scope
        and returns a tensor of shape (batch_size, num_actions) with values of every action.
    :param learning_rate: (float) learning rate for adam optimizer
    :param max_timesteps: (int) number of env steps to optimizer for
    :param buffer_size: (int) size of the replay buffer
    :param exploration_fraction: (float) fraction of entire training period over which the exploration rate is annealed
    :param exploration_final_eps: (float) final value of random action probability
    :param train_freq: (int) update the model every `train_freq` steps. set to None to disable printing
    :param batch_size: (int) size of a batched sampled from replay buffer for training
    :param print_freq: (int) how often to print out training progress set to None to disable printing
    :param checkpoint_freq: (int) how often to save the model. This is so that the best version is restored at the end
        of the training. If you do not wish to restore the best version at the end of the training set this variable
        to None.
    :param checkpoint_path: (str) replacement path used if you need to log to somewhere else than a temporary directory.
    :param learning_starts: (int) how many steps of the model to collect transitions for before learning starts
    :param gamma: (float) discount factor
    :param target_network_update_freq: (int) update the target network every `target_network_update_freq` steps.
    :param prioritized_replay: (bool) if True prioritized replay buffer will be used.
    :param prioritized_replay_alpha: (float) alpha parameter for prioritized replay buffer
    :param prioritized_replay_beta0: (float) initial value of beta for prioritized replay buffer
    :param prioritized_replay_beta_iters: (int) number of iterations over which beta will be annealed from initial value
        to 1.0. If set to None equals to max_timesteps.
    :param prioritized_replay_eps: (float) epsilon to add to the TD errors when updating priorities.
    :param param_noise: (bool) Whether or not to apply noise to the parameters of the policy.
    :param callback: (function (dict, dict)) function called at every steps with state of the algorithm.
        If callback returns true training stops. It takes the local and global variables.
    :return: (ActWrapper) Wrapper over act function. Adds ability to save it and load it. See header of
        baselines/deepq/categorical.py for details on the act function.
    """
    # Create all the functions necessary to train the model

    # capture the shape outside the closure so that the env object is not serialized
    # by cloudpickle when serializing make_obs_ph
    observation_space_shape = env.observation_space

    def make_obs_ph(name):
        """
        makes the observation placeholder

        :param name: (str) the placeholder name
        :return: (TensorFlow Tensor) the placeholder
        """
        return ObservationInput(observation_space_shape, name=name)

    act, train, update_target, _ = deepq.build_train(
        make_obs_ph=make_obs_ph,
        q_func=q_func,
        num_actions=env.action_space.n,
        optimizer=tf.train.AdamOptimizer(learning_rate=learning_rate),
        gamma=gamma,
        grad_norm_clipping=10,
        param_noise=param_noise
    )

    act_params = {
        'make_obs_ph': make_obs_ph,
        'q_func': q_func,
        'num_actions': env.action_space.n,
    }

    act = ActWrapper(act, act_params)

    # Create the replay buffer
    if prioritized_replay:
        replay_buffer = PrioritizedReplayBuffer(buffer_size, alpha=prioritized_replay_alpha)
        if prioritized_replay_beta_iters is None:
            prioritized_replay_beta_iters = max_timesteps
        beta_schedule = LinearSchedule(prioritized_replay_beta_iters,
                                       initial_p=prioritized_replay_beta0,
                                       final_p=1.0)
    else:
        replay_buffer = ReplayBuffer(buffer_size)
        beta_schedule = None
    # Create the schedule for exploration starting from 1.
    exploration = LinearSchedule(schedule_timesteps=int(exploration_fraction * max_timesteps),
                                 initial_p=1.0,
                                 final_p=exploration_final_eps)

    # Initialize the parameters and copy them to the target network.
    tf_util.initialize(act.sess)
    update_target(sess=act.sess)

    episode_rewards = [0.0]
    saved_mean_reward = None
    obs = env.reset()
    reset = True

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = checkpoint_path or temp_dir

        model_file = os.path.join(temp_dir, "model")
        model_saved = False
        if tf.train.latest_checkpoint(temp_dir) is not None:
            load_state(model_file, act.sess)
            logger.log('Loaded model from {}'.format(model_file))
            model_saved = True

        for step in range(max_timesteps):
            if callback is not None:
                if callback(locals(), globals()):
                    break
            # Take action and update exploration to the newest value
            kwargs = {}
            if not param_noise:
                update_eps = exploration.value(step)
                update_param_noise_threshold = 0.
            else:
                update_eps = 0.
                # Compute the threshold such that the KL divergence between perturbed and non-perturbed
                # policy is comparable to eps-greedy exploration with eps = exploration.value(t).
                # See Appendix C.1 in Parameter Space Noise for Exploration, Plappert et al., 2017
                # for detailed explanation.
                update_param_noise_threshold = -np.log(1. - exploration.value(step) +
                                                       exploration.value(step) / float(env.action_space.n))
                kwargs['reset'] = reset
                kwargs['update_param_noise_threshold'] = update_param_noise_threshold
                kwargs['update_param_noise_scale'] = True
            action = act(np.array(obs)[None], update_eps=update_eps, **kwargs)[0]
            env_action = action
            reset = False
            new_obs, rew, done, _ = env.step(env_action)
            # Store transition in the replay buffer.
            replay_buffer.add(obs, action, rew, new_obs, float(done))
            obs = new_obs

            episode_rewards[-1] += rew
            if done:
                obs = env.reset()
                episode_rewards.append(0.0)
                reset = True

            if step > learning_starts and step % train_freq == 0:
                # Minimize the error in Bellman's equation on a batch sampled from replay buffer.
                if prioritized_replay:
                    experience = replay_buffer.sample(batch_size, beta=beta_schedule.value(step))
                    (obses_t, actions, rewards, obses_tp1, dones, weights, batch_idxes) = experience
                else:
                    obses_t, actions, rewards, obses_tp1, dones = replay_buffer.sample(batch_size)
                    weights, batch_idxes = np.ones_like(rewards), None
                td_errors = train(obses_t, actions, rewards, obses_tp1, dones, weights, sess=act.sess)
                if prioritized_replay:
                    new_priorities = np.abs(td_errors) + prioritized_replay_eps
                    replay_buffer.update_priorities(batch_idxes, new_priorities)

            if step > learning_starts and step % target_network_update_freq == 0:
                # Update target network periodically.
                update_target(sess=act.sess)

            if len(episode_rewards[-101:-1]) == 0:
                mean_100ep_reward = -np.inf
            else:
                mean_100ep_reward = round(float(np.mean(episode_rewards[-101:-1])), 1)

            num_episodes = len(episode_rewards)
            if done and print_freq is not None and len(episode_rewards) % print_freq == 0:
                logger.record_tabular("steps", step)
                logger.record_tabular("episodes", num_episodes)
                logger.record_tabular("mean 100 episode reward", mean_100ep_reward)
                logger.record_tabular("% time spent exploring", int(100 * exploration.value(step)))
                logger.dump_tabular()

            if (checkpoint_freq is not None and step > learning_starts and
                    num_episodes > 100 and step % checkpoint_freq == 0):
                if saved_mean_reward is None or mean_100ep_reward > saved_mean_reward:
                    if print_freq is not None:
                        logger.log("Saving model due to mean reward increase: {} -> {}".format(
                                   saved_mean_reward, mean_100ep_reward))
                    save_state(model_file, act.sess)
                    model_saved = True
                    saved_mean_reward = mean_100ep_reward
        if model_saved:
            if print_freq is not None:
                logger.log("Restored model with mean reward: {}".format(saved_mean_reward))
            load_state(model_file, act.sess)

    return act
