import time
import joblib
import os

import numpy as np
import tensorflow as tf

from baselines import logger
from baselines.common import set_global_seeds
from baselines.common.runners import AbstractEnvRunner
from baselines.acer.buffer import Buffer
from baselines.a2c.utils import batch_to_seq, seq_to_batch, Scheduler, make_path, find_trainable_variables, \
    calc_entropy_softmax, EpisodeStats, get_by_index, check_shape, avg_norm, gradient_add, q_explained_variance


def strip(var, n_envs, n_steps, flat=False):
    """
    Removes the last step in the batch

    :param var: (TensorFlow Tensor) The input Tensor
    :param n_envs: (int) The number of environments
    :param n_steps: (int) The number of steps to run for each environment
    :param flat: (bool) If the input Tensor is flat
    :return: (TensorFlow Tensor) the input tensor, without the last step in the batch
    """
    out_vars = batch_to_seq(var, n_envs, n_steps + 1, flat)
    return seq_to_batch(out_vars[:-1], flat)


def q_retrace(rewards, dones, q_i, values, rho_i, n_envs, n_steps, gamma):
    """
    Calculates the target Q-retrace

    :param rewards: ([TensorFlow Tensor]) The rewards
    :param dones: ([TensorFlow Tensor])
    :param q_i: ([TensorFlow Tensor]) The Q values for actions taken
    :param values: ([TensorFlow Tensor]) The output of the value functions
    :param rho_i: ([TensorFlow Tensor]) The importance weight for each action
    :param n_envs: (int) The number of environments
    :param n_steps: (int) The number of steps to run for each environment
    :param gamma: (float) The discount value
    :return: ([TensorFlow Tensor]) the target Q-retrace
    """
    rho_bar = batch_to_seq(tf.minimum(1.0, rho_i), n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    reward_seq = batch_to_seq(rewards, n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    done_seq = batch_to_seq(dones, n_envs, n_steps, True)  # list of len steps, shape [n_envs]
    q_is = batch_to_seq(q_i, n_envs, n_steps, True)
    value_sequence = batch_to_seq(values, n_envs, n_steps + 1, True)
    final_value = value_sequence[-1]
    qret = final_value
    qrets = []
    for i in range(n_steps - 1, -1, -1):
        check_shape([qret, done_seq[i], reward_seq[i], rho_bar[i], q_is[i], value_sequence[i]], [[n_envs]] * 6)
        qret = reward_seq[i] + gamma * qret * (1.0 - done_seq[i])
        qrets.append(qret)
        qret = (rho_bar[i] * (qret - q_is[i])) + value_sequence[i]
    qrets = qrets[::-1]
    qret = seq_to_batch(qrets, flat=True)
    return qret


class Model(object):
    def __init__(self, policy, ob_space, ac_space, n_envs, n_steps, n_stack, num_procs, ent_coef, q_coef, gamma,
                 max_grad_norm, learning_rate, rprop_alpha, rprop_epsilon,
                 total_timesteps, lr_schedule, correction_term, trust_region, alpha, delta):
        """
        The ACER (Actor-Critic with Experience Replay) model class, https://arxiv.org/abs/1611.01224

        :param policy: (AcerPolicy) The policy model to use (MLP, CNN, LSTM, ...)
        :param ob_space: (Gym Space) The observation space
        :param ac_space: (Gym Space) The action space
        :param n_envs: (int) The number of environments
        :param n_steps: (int) The number of steps to run for each environment
        :param n_stack: (int) The number of stacked frames
        :param num_procs: (int) The number of threads for TensorFlow operations
        :param ent_coef: (float) The weight for the entropic loss
        :param q_coef: (float) The weight for the loss on the Q value
        :param gamma: (float) The discount value
        :param max_grad_norm: (float) The clipping value for the maximum gradient
        :param learning_rate: (float) The initial learning rate for the RMS prop optimizer
        :param rprop_alpha: (float) RMS prop optimizer decay rate
        :param rprop_epsilon: (float) RMS prop optimizer epsilon
        :param total_timesteps: (int) The total number of timesteps for training the model
        :param lr_schedule: (str) The scheduler for a dynamic learning rate
        :param correction_term: (float) The correction term for the weights
        :param trust_region: (bool) Enable Trust region policy optimization loss
        :param alpha: (float) The decay rate for the Exponential moving average of the parameters
        :param delta: (float) trust region delta value
        """
        config = tf.ConfigProto(allow_soft_placement=True,
                                intra_op_parallelism_threads=num_procs,
                                inter_op_parallelism_threads=num_procs)
        sess = tf.Session(config=config)
        n_act = ac_space.n
        n_batch = n_envs * n_steps

        action_ph = tf.placeholder(tf.int32, [n_batch])  # actions
        done_ph = tf.placeholder(tf.float32, [n_batch])  # dones
        reward_ph = tf.placeholder(tf.float32, [n_batch])  # rewards, not returns
        mu_ph = tf.placeholder(tf.float32, [n_batch, n_act])  # mu's
        learning_rate_ph = tf.placeholder(tf.float32, [])
        eps = 1e-6

        step_model = policy(sess, ob_space, ac_space, n_envs, 1, n_stack, reuse=False)
        train_model = policy(sess, ob_space, ac_space, n_envs, n_steps + 1, n_stack, reuse=True)

        params = find_trainable_variables("model")
        print("Params {}".format(len(params)))
        for var in params:
            print(var)

        # create polyak averaged model
        ema = tf.train.ExponentialMovingAverage(alpha)
        ema_apply_op = ema.apply(params)

        def custom_getter(getter, *args, **kwargs):
            val = ema.average(getter(*args, **kwargs))
            print(val.name)
            return val

        with tf.variable_scope("", custom_getter=custom_getter, reuse=True):
            polyak_model = policy(sess, ob_space, ac_space, n_envs, n_steps + 1, n_stack, reuse=True)

        # Notation: (var) = batch variable, (var)s = sequence variable, (var)_i = variable index by action at step i
        value = tf.reduce_sum(train_model.policy * train_model.q_value, axis=-1)  # shape is [n_envs * (n_steps + 1)]

        # strip off last step
        # f is a distribution, chosen to be Gaussian distributions
        # with fixed diagonal covariance and mean \phi(x)
        # in the paper
        distribution_f, f_polyak, q_value = map(lambda variables: strip(variables, n_envs, n_steps),
                                                [train_model.policy, polyak_model.policy, train_model.q_value])
        # Get pi and q values for actions taken
        f_i = get_by_index(distribution_f, action_ph)
        q_i = get_by_index(q_value, action_ph)

        # Compute ratios for importance truncation
        rho = distribution_f / (mu_ph + eps)
        rho_i = get_by_index(rho, action_ph)

        # Calculate Q_retrace targets
        qret = q_retrace(reward_ph, done_ph, q_i, value, rho_i, n_envs, n_steps, gamma)

        # Calculate losses
        # Entropy
        entropy = tf.reduce_mean(calc_entropy_softmax(distribution_f))

        # Policy Gradient loss, with truncated importance sampling & bias correction
        value = strip(value, n_envs, n_steps, True)
        check_shape([qret, value, rho_i, f_i], [[n_envs * n_steps]] * 4)
        check_shape([rho, distribution_f, q_value], [[n_envs * n_steps, n_act]] * 2)

        # Truncated importance sampling
        adv = qret - value
        log_f = tf.log(f_i + eps)
        gain_f = log_f * tf.stop_gradient(adv * tf.minimum(correction_term, rho_i))  # [n_envs * n_steps]
        loss_f = -tf.reduce_mean(gain_f)

        # Bias correction for the truncation
        adv_bc = (q_value - tf.reshape(value, [n_envs * n_steps, 1]))  # [n_envs * n_steps, n_act]
        log_f_bc = tf.log(distribution_f + eps)  # / (f_old + eps)
        check_shape([adv_bc, log_f_bc], [[n_envs * n_steps, n_act]] * 2)
        gain_bc = tf.reduce_sum(log_f_bc *
                                tf.stop_gradient(
                                    adv_bc * tf.nn.relu(1.0 - (correction_term / (rho + eps))) * distribution_f),
                                axis=1)
        # IMP: This is sum, as expectation wrt f
        loss_bc = -tf.reduce_mean(gain_bc)

        loss_policy = loss_f + loss_bc

        # Value/Q function loss, and explained variance
        check_shape([qret, q_i], [[n_envs * n_steps]] * 2)
        explained_variance = q_explained_variance(tf.reshape(q_i, [n_envs, n_steps]),
                                                  tf.reshape(qret, [n_envs, n_steps]))
        loss_q = tf.reduce_mean(tf.square(tf.stop_gradient(qret) - q_i) * 0.5)

        # Net loss
        check_shape([loss_policy, loss_q, entropy], [[]] * 3)
        loss = loss_policy + q_coef * loss_q - ent_coef * entropy

        if trust_region:
            # [n_envs * n_steps, n_act]
            grad = tf.gradients(- (loss_policy - ent_coef * entropy) * n_steps * n_envs, distribution_f)
            # [n_envs * n_steps, n_act] # Directly computed gradient of KL divergence wrt f
            kl_grad = - f_polyak / (distribution_f + eps)
            k_dot_g = tf.reduce_sum(kl_grad * grad, axis=-1)
            adj = tf.maximum(0.0, (tf.reduce_sum(kl_grad * grad, axis=-1) - delta) / (
                    tf.reduce_sum(tf.square(kl_grad), axis=-1) + eps))  # [n_envs * n_steps]

            # Calculate stats (before doing adjustment) for logging.
            avg_norm_k = avg_norm(kl_grad)
            avg_norm_g = avg_norm(grad)
            avg_norm_k_dot_g = tf.reduce_mean(tf.abs(k_dot_g))
            avg_norm_adj = tf.reduce_mean(tf.abs(adj))

            grad = grad - tf.reshape(adj, [n_envs * n_steps, 1]) * kl_grad
            grads_f = -grad / (
                    n_envs * n_steps)  # These are turst region adjusted gradients wrt f ie statistics of policy pi
            grads_policy = tf.gradients(distribution_f, params, grads_f)
            grads_q = tf.gradients(loss_q * q_coef, params)
            grads = [gradient_add(g1, g2, param) for (g1, g2, param) in zip(grads_policy, grads_q, params)]

            avg_norm_grads_f = avg_norm(grads_f) * (n_steps * n_envs)
            norm_grads_q = tf.global_norm(grads_q)
            norm_grads_policy = tf.global_norm(grads_policy)
        else:
            grads = tf.gradients(loss, params)

        if max_grad_norm is not None:
            grads, norm_grads = tf.clip_by_global_norm(grads, max_grad_norm)
        grads = list(zip(grads, params))
        trainer = tf.train.RMSPropOptimizer(learning_rate=learning_rate_ph, decay=rprop_alpha, epsilon=rprop_epsilon)
        _opt_op = trainer.apply_gradients(grads)

        # so when you call _train, you first do the gradient step, then you apply ema
        with tf.control_dependencies([_opt_op]):
            _train = tf.group(ema_apply_op)

        learning_rate = Scheduler(initial_value=learning_rate, n_values=total_timesteps, schedule=lr_schedule)

        # Ops/Summaries to run, and their names for logging
        run_ops = [_train, loss, loss_q, entropy, loss_policy, loss_f, loss_bc, explained_variance, norm_grads]
        names_ops = ['loss', 'loss_q', 'entropy', 'loss_policy', 'loss_f', 'loss_bc', 'explained_variance',
                     'norm_grads']
        if trust_region:
            run_ops = run_ops + [norm_grads_q, norm_grads_policy, avg_norm_grads_f, avg_norm_k, avg_norm_g,
                                 avg_norm_k_dot_g,
                                 avg_norm_adj]
            names_ops = names_ops + ['norm_grads_q', 'norm_grads_policy', 'avg_norm_grads_f', 'avg_norm_k',
                                     'avg_norm_g',
                                     'avg_norm_k_dot_g', 'avg_norm_adj']

        def train(obs, actions, rewards, dones, mus, states, masks, steps):
            cur_lr = learning_rate.value_steps(steps)
            td_map = {train_model.obs_ph: obs, polyak_model.obs_ph: obs, action_ph: actions, reward_ph: rewards,
                      done_ph: dones, mu_ph: mus, learning_rate_ph: cur_lr}

            if len(states) != 0:
                td_map[train_model.states_ph] = states
                td_map[train_model.masks_ph] = masks
                td_map[polyak_model.states_ph] = states
                td_map[polyak_model.masks_ph] = masks

            return names_ops, sess.run(run_ops, td_map)[1:]  # strip off _train

        def save(save_path):
            session_params = sess.run(params)
            make_path(os.path.dirname(save_path))
            joblib.dump(session_params, save_path)

        self.train = train
        self.save = save
        self.train_model = train_model
        self.step_model = step_model
        self.step = step_model.step
        self.initial_state = step_model.initial_state
        tf.global_variables_initializer().run(session=sess)


class Runner(AbstractEnvRunner):
    def __init__(self, env, model, n_steps, n_stack):
        """
        A runner to learn the policy of an environment for a model

        :param env: (Gym environment) The environment to learn from
        :param model: (Model) The model to learn
        :param n_steps: (int) The number of steps to run for each environment
        :param n_stack: (int) The number of stacked frames
        """
        super().__init__(env=env, model=model, n_steps=n_steps)
        self.n_stack = n_stack
        obs_height, obs_width, obs_num_channels = env.observation_space.shape
        self.num_channels = obs_num_channels  # obs_num_channels = 1 for atari, but just in case
        self.n_env = n_env = env.num_envs
        self.n_act = env.action_space.n
        self.n_batch = n_env * n_steps
        self.batch_ob_shape = (n_env * (n_steps + 1), obs_height, obs_width, obs_num_channels * n_stack)
        self.obs = np.zeros((n_env, obs_height, obs_width, obs_num_channels * n_stack), dtype=np.uint8)
        obs = env.reset()
        self.update_obs(obs)

    def update_obs(self, obs, dones=None):
        """
        Update the observation for rolling observation with stacking

        :param obs: ([int] or [float]) The input observation
        :param dones: ([bool])
        """
        if dones is not None:
            self.obs *= (1 - dones.astype(np.uint8))[:, None, None, None]
        self.obs = np.roll(self.obs, shift=-self.num_channels, axis=3)
        self.obs[:, :, :, -self.num_channels:] = obs[:, :, :, :]

    def run(self):
        """
        Run a step leaning of the model

        :return: ([float], [float], [float], [float], [float], [bool], [float])
                 encoded observation, observations, actions, rewards, mus, dones, masks
        """
        enc_obs = np.split(self.obs, self.n_stack, axis=3)  # so now list of obs steps
        mb_obs, mb_actions, mb_mus, mb_dones, mb_rewards = [], [], [], [], []
        for _ in range(self.n_steps):
            actions, mus, states = self.model.step(self.obs, state=self.states, mask=self.dones)
            mb_obs.append(np.copy(self.obs))
            mb_actions.append(actions)
            mb_mus.append(mus)
            mb_dones.append(self.dones)
            obs, rewards, dones, _ = self.env.step(actions)
            # states information for statefull models like LSTM
            self.states = states
            self.dones = dones
            self.update_obs(obs, dones)
            mb_rewards.append(rewards)
            enc_obs.append(obs)
        mb_obs.append(np.copy(self.obs))
        mb_dones.append(self.dones)

        enc_obs = np.asarray(enc_obs, dtype=np.uint8).swapaxes(1, 0)
        mb_obs = np.asarray(mb_obs, dtype=np.uint8).swapaxes(1, 0)
        mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
        mb_mus = np.asarray(mb_mus, dtype=np.float32).swapaxes(1, 0)

        mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)

        mb_masks = mb_dones  # Used for statefull models like LSTM's to mask state when done
        mb_dones = mb_dones[:, 1:]  # Used for calculating returns. The dones array is now aligned with rewards

        # shapes are now [n_env, n_steps, []]
        # When pulling from buffer, arrays will now be reshaped in place, preventing a deep copy.

        return enc_obs, mb_obs, mb_actions, mb_rewards, mb_mus, mb_dones, mb_masks


class Acer(object):
    def __init__(self, runner, model, buffer, log_interval):
        """
        Wrapper for the ACER model object

        :param runner: (AbstractEnvRunner) The runner to learn the policy of an environment for a model
        :param model: (Model) The model to learn
        :param buffer: (Buffer) The observation buffer
        :param log_interval: (int) The number of timesteps before logging.
        """
        super(Acer, self).__init__()
        self.runner = runner
        self.model = model
        self.buffer = buffer
        self.log_interval = log_interval
        self.t_start = None
        self.episode_stats = EpisodeStats(runner.n_steps, runner.n_env)
        self.steps = None

    def call(self, on_policy):
        """
        Call a step with ACER

        :param on_policy: (bool) To step on policy and not on buffer
        """
        runner, model, buffer, steps = self.runner, self.model, self.buffer, self.steps
        if on_policy:
            enc_obs, obs, actions, rewards, mus, dones, masks = runner.run()
            self.episode_stats.feed(rewards, dones)
            if buffer is not None:
                buffer.put(enc_obs, actions, rewards, mus, dones, masks)
        else:
            # get obs, actions, rewards, mus, dones from buffer.
            obs, actions, rewards, mus, dones, masks = buffer.get()

        # reshape stuff correctly
        obs = obs.reshape(runner.batch_ob_shape)
        actions = actions.reshape([runner.n_batch])
        rewards = rewards.reshape([runner.n_batch])
        mus = mus.reshape([runner.n_batch, runner.n_act])
        dones = dones.reshape([runner.n_batch])
        masks = masks.reshape([runner.batch_ob_shape[0]])

        names_ops, values_ops = model.train(obs, actions, rewards, dones, mus, model.initial_state, masks, steps)

        if on_policy and (int(steps / runner.n_batch) % self.log_interval == 0):
            logger.record_tabular("total_timesteps", steps)
            logger.record_tabular("fps", int(steps / (time.time() - self.t_start)))
            # IMP: In EpisodicLife env, during training, we get done=True at each loss of life,
            # not just at the terminal state. Thus, this is mean until end of life, not end of episode.
            # For true episode rewards, see the monitor files in the log folder.
            logger.record_tabular("mean_episode_length", self.episode_stats.mean_length())
            logger.record_tabular("mean_episode_reward", self.episode_stats.mean_reward())
            for name, val in zip(names_ops, values_ops):
                logger.record_tabular(name, float(val))
            logger.dump_tabular()


def learn(policy, env, seed, n_steps=20, n_stack=4, total_timesteps=int(80e6),
          q_coef=0.5, ent_coef=0.01,
          max_grad_norm=10, learning_rate=7e-4, lr_schedule='linear',
          rprop_epsilon=1e-5, rprop_alpha=0.99, gamma=0.99,
          log_interval=100, buffer_size=50000, replay_ratio=4,
          replay_start=10000, correction_term=10.0,
          trust_region=True, alpha=0.99, delta=1):
    """
    Train an ACER model.

    :param policy: (ACERPolicy) The policy model to use (MLP, CNN, LSTM, ...)
    :param env: (Gym environment) The environment to learn from
    :param seed: (int) The initial seed for training
    :param n_steps: (int) The number of steps to run for each environment
    :param n_stack: (int) The number of stacked frames
    :param total_timesteps: (int) The total number of samples
    :param q_coef: (float) Q function coefficient for the loss calculation
    :param ent_coef: (float) Entropy coefficient for the loss caculation
    :param max_grad_norm: (float) The maximum value for the gradient clipping
    :param learning_rate: (float) The learning rate
    :param lr_schedule: (str) The type of scheduler for the learning rate update ('linear', 'constant',
                                 'double_linear_con', 'middle_drop' or 'double_middle_drop')
    :param rprop_epsilon: (float) RMS prop optimizer epsilon
    :param rprop_alpha: (float) RMS prop optimizer decay
    :param gamma: (float) Discount factor
    :param log_interval: (int) The number of timesteps before logging.
    :param buffer_size: (int) The buffer size in number of steps
    :param replay_ratio: (float) The number of replay learning per on policy learning on average,
                                 using a poisson distribution
    :param replay_start: (int) The minimum number of steps in the buffer, before learning replay
    :param correction_term: (float) The correction term for the weights
    :param trust_region: (bool) Enable Trust region policy optimization loss
    :param alpha: (float) The decay rate for the Exponential moving average of the parameters
    :param delta: (float) trust region delta value
    """
    print("Running Acer Simple")
    print(locals())
    set_global_seeds(seed)

    n_envs = env.num_envs
    ob_space = env.observation_space
    ac_space = env.action_space
    num_procs = len(env.remotes)  # HACK
    model = Model(policy=policy, ob_space=ob_space, ac_space=ac_space, n_envs=n_envs, n_steps=n_steps, n_stack=n_stack,
                  num_procs=num_procs, ent_coef=ent_coef, q_coef=q_coef, gamma=gamma,
                  max_grad_norm=max_grad_norm, learning_rate=learning_rate, rprop_alpha=rprop_alpha,
                  rprop_epsilon=rprop_epsilon,
                  total_timesteps=total_timesteps, lr_schedule=lr_schedule, correction_term=correction_term,
                  trust_region=trust_region, alpha=alpha, delta=delta)

    runner = Runner(env=env, model=model, n_steps=n_steps, n_stack=n_stack)
    if replay_ratio > 0:
        buffer = Buffer(env=env, n_steps=n_steps, n_stack=n_stack, size=buffer_size)
    else:
        buffer = None
    n_batch = n_envs * n_steps
    acer = Acer(runner, model, buffer, log_interval)
    acer.t_start = time.time()
    for acer.steps in range(0, total_timesteps,
                            n_batch):  # n_batch samples, 1 on_policy call and multiple off-policy calls
        acer.call(on_policy=True)
        if replay_ratio > 0 and buffer.has_atleast(replay_start):
            samples_number = np.random.poisson(replay_ratio)
            for _ in range(samples_number):
                acer.call(on_policy=False)  # no simulation steps in this

    env.close()
