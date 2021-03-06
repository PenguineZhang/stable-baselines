import time
import os
from contextlib import contextmanager
from collections import deque

from mpi4py import MPI
import tensorflow as tf
import numpy as np

import baselines.common.tf_util as tf_util
from baselines.common import explained_variance, zipsame, dataset, fmt_row, colorize
from baselines import logger
from baselines.common.mpi_adam import MpiAdam
from baselines.common.cg import conjugate_gradient


# from baselines.gail.statistics import Stats


def traj_segment_generator(policy, env, horizon, stochastic, reward_giver=None, gail=False):
    """
    Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)

    :param policy: (MLPPolicy) the policy
    :param env: (Gym Environment) the environment
    :param horizon: (int) the number of timesteps to run per batch
    :param stochastic: (bool) use a stochastic policy
    :param reward_giver: (TransitionClassifier) the reward predicter from obsevation and action
    :param gail: (bool) Whether we are using this generator for standard trpo or with gail
    :return: (dict) generator that returns a dict with the following keys:

        - ob: (numpy Number) observations
        - rew: (numpy float) rewards (if gail is used it is the predicted reward)
        - vpred: (numpy float) action logits
        - new: (numpy bool) dones (is end of episode)
        - ac: (numpy Number) actions
        - prevac: (numpy Number) previous actions
        - nextvpred: (numpy float) next action logits
        - ep_rets: (float) cumulated current episode reward
        - ep_lens: (int) the length of the current episode
        - ep_true_rets: (float) the real environment reward
    """
    # Check when using GAIL
    assert not (gail and reward_giver is None), "You must pass a reward giver when using GAIL"

    # Initialize state variables
    step = 0
    action = env.action_space.sample()  # not used, just so we have the datatype
    done = True
    observation = env.reset()

    cur_ep_ret = 0  # return in current episode
    cur_ep_len = 0  # len of current episode
    cur_ep_true_ret = 0
    ep_true_rets = []
    ep_rets = []  # returns of completed episodes in this segment
    ep_lens = []  # Episode lengths

    # Initialize history arrays
    observations = np.array([observation for _ in range(horizon)])
    true_rewards = np.zeros(horizon, 'float32')
    rewards = np.zeros(horizon, 'float32')
    vpreds = np.zeros(horizon, 'float32')
    dones = np.zeros(horizon, 'int32')
    actions = np.array([action for _ in range(horizon)])
    prev_actions = actions.copy()

    while True:
        prevac = action
        action, vpred = policy.act(stochastic, observation)
        # Slight weirdness here because we need value function at time T
        # before returning segment [0, T-1] so we get the correct
        # terminal value
        if step > 0 and step % horizon == 0:
            yield {"ob": observations, "rew": rewards, "vpred": vpreds, "new": dones,
                   "ac": actions, "prevac": prev_actions, "nextvpred": vpred * (1 - done),
                   "ep_rets": ep_rets, "ep_lens": ep_lens, "ep_true_rets": ep_true_rets}
            _, vpred = policy.act(stochastic, observation)
            # Be careful!!! if you change the downstream algorithm to aggregate
            # several of these batches, then be sure to do a deepcopy
            ep_rets = []
            ep_true_rets = []
            ep_lens = []
        idx = step % horizon
        observations[idx] = observation
        vpreds[idx] = vpred
        dones[idx] = done
        actions[idx] = action
        prev_actions[idx] = prevac

        if gail:
            reward = reward_giver.get_reward(observation, action)
            observation, true_reward, done, _ = env.step(action)
        else:
            observation, reward, done, _ = env.step(action)
            true_reward = reward
        rewards[idx] = reward
        true_rewards[idx] = true_reward

        cur_ep_ret += reward
        cur_ep_true_ret += true_reward
        cur_ep_len += 1
        if done:
            ep_rets.append(cur_ep_ret)
            ep_true_rets.append(cur_ep_true_ret)
            ep_lens.append(cur_ep_len)
            cur_ep_ret = 0
            cur_ep_true_ret = 0
            cur_ep_len = 0
            observation = env.reset()
        step += 1


def add_vtarg_and_adv(seg, gamma, lam):
    """
    Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)

    :param seg: (dict) the current segment of the trajectory (see traj_segment_generator return for more information)
    :param gamma: (float) Discount factor
    :param lam: (float) GAE factor
    """
    # last element is only used for last vtarg, but we already zeroed it if last done = 1
    done = np.append(seg["new"], 0)
    vpred = np.append(seg["vpred"], seg["nextvpred"])
    time_horizon = len(seg["rew"])
    seg["adv"] = gae_lam = np.empty(time_horizon, 'float32')
    rew = seg["rew"]
    last_gae_lam = 0
    for step in reversed(range(time_horizon)):
        non_terminal = 1 - done[step + 1]
        delta = rew[step] + gamma * vpred[step + 1] * non_terminal - vpred[step]
        gae_lam[step] = last_gae_lam = delta + gamma * lam * non_terminal * last_gae_lam
    seg["tdlamret"] = seg["adv"] + seg["vpred"]


def learn(env, policy_func, *, timesteps_per_batch, max_kl, cg_iters, gamma, lam, entcoeff=0.0, cg_damping=1e-2,
          vf_stepsize=3e-4, vf_iters=3, max_timesteps=0, max_episodes=0, max_iters=0, callback=None,
          # GAIL Params
          pretrained_weight=None, reward_giver=None, expert_dataset=None, rank=0, save_per_iter=1,
          ckpt_dir="/tmp/gail/ckpt/", g_step=1, d_step=1, task_name="task_name", d_stepsize=3e-4, using_gail=True):
    """
    learns a GAIL policy using the given environment

    :param env: (Gym Environment) the environment
    :param policy_func: (function (str, Gym Space, Gym Space, bool): MLPPolicy) policy generator
    :param timesteps_per_batch: (int) the number of timesteps to run per batch (horizon)
    :param max_kl: (float) the kullback leiber loss threashold
    :param cg_iters: (int) the number of iterations for the conjugate gradient calculation
    :param gamma: (float) the discount value
    :param lam: (float) GAE factor
    :param entcoeff: (float) the weight for the entropy loss
    :param cg_damping: (float) the compute gradient dampening factor
    :param vf_stepsize: (float) the value function stepsize
    :param vf_iters: (int) the value function's number iterations for learning
    :param max_timesteps: (int) the maximum number of timesteps before halting
    :param max_episodes: (int) the maximum number of episodes before halting
    :param max_iters: (int) the maximum number of training iterations  before halting
    :param callback: (function (dict, dict)) the call back function, takes the local and global attribute dictionary
    :param pretrained_weight: (str) the save location for the pretrained weights
    :param reward_giver: (TransitionClassifier) the reward predicter from obsevation and action
    :param expert_dataset: (MujocoDset) the dataset manager
    :param rank: (int) the rank of the mpi thread
    :param save_per_iter: (int) the number of iterations before saving
    :param ckpt_dir: (str) the location for saving checkpoints
    :param g_step: (int) number of steps to train policy in each epoch
    :param d_step: (int) number of steps to train discriminator in each epoch
    :param task_name: (str) the name of the task (can be None)
    :param d_stepsize: (float) the reward giver stepsize
    :param using_gail: (bool) using the GAIL model
    """

    nworkers = MPI.COMM_WORLD.Get_size()
    rank = MPI.COMM_WORLD.Get_rank()
    np.set_printoptions(precision=3)
    sess = tf_util.single_threaded_session()
    # Setup losses and stuff
    # ----------------------------------------
    ob_space = env.observation_space
    ac_space = env.action_space
    policy = policy_func("pi", ob_space, ac_space, sess=sess)
    old_policy = policy_func("oldpi", ob_space, ac_space, sess=sess,
                             placeholders={"obs": policy.obs_ph, "stochastic": policy.stochastic_ph})

    atarg = tf.placeholder(dtype=tf.float32, shape=[None])  # Target advantage function (if applicable)
    ret = tf.placeholder(dtype=tf.float32, shape=[None])  # Empirical return

    observation = policy.obs_ph
    action = policy.pdtype.sample_placeholder([None])

    kloldnew = old_policy.proba_distribution.kl(policy.proba_distribution)
    ent = policy.proba_distribution.entropy()
    meankl = tf.reduce_mean(kloldnew)
    meanent = tf.reduce_mean(ent)
    entbonus = entcoeff * meanent

    vferr = tf.reduce_mean(tf.square(policy.vpred - ret))

    # advantage * pnew / pold
    ratio = tf.exp(policy.proba_distribution.logp(action) - old_policy.proba_distribution.logp(action))
    surrgain = tf.reduce_mean(ratio * atarg)

    optimgain = surrgain + entbonus
    losses = [optimgain, meankl, entbonus, surrgain, meanent]
    loss_names = ["optimgain", "meankl", "entloss", "surrgain", "entropy"]

    dist = meankl

    all_var_list = policy.get_trainable_variables()
    if using_gail:
        var_list = [v for v in all_var_list if v.name.startswith("pi/pol") or v.name.startswith("pi/logstd")]
        vf_var_list = [v for v in all_var_list if v.name.startswith("pi/vff")]
        assert len(var_list) == len(vf_var_list) + 1
        d_adam = MpiAdam(reward_giver.get_trainable_variables())
    else:
        var_list = [v for v in all_var_list if v.name.split("/")[1].startswith("pol")]
        vf_var_list = [v for v in all_var_list if v.name.split("/")[1].startswith("vf")]

    vfadam = MpiAdam(vf_var_list, sess=sess)
    get_flat = tf_util.GetFlat(var_list, sess=sess)
    set_from_flat = tf_util.SetFromFlat(var_list, sess=sess)

    klgrads = tf.gradients(dist, var_list)
    flat_tangent = tf.placeholder(dtype=tf.float32, shape=[None], name="flat_tan")
    shapes = [var.get_shape().as_list() for var in var_list]
    start = 0
    tangents = []
    for shape in shapes:
        var_size = tf_util.intprod(shape)
        tangents.append(tf.reshape(flat_tangent[start: start + var_size], shape))
        start += var_size
    gvp = tf.add_n(
        [tf.reduce_sum(grad * tangent) for (grad, tangent) in zipsame(klgrads, tangents)])  # pylint: disable=E1111
    fvp = tf_util.flatgrad(gvp, var_list)

    assign_old_eq_new = tf_util.function([], [], updates=[tf.assign(oldv, newv) for (oldv, newv) in
                                                          zipsame(old_policy.get_variables(), policy.get_variables())])
    compute_losses = tf_util.function([observation, action, atarg], losses)
    compute_lossandgrad = tf_util.function([observation, action, atarg],
                                           losses + [tf_util.flatgrad(optimgain, var_list)])
    compute_fvp = tf_util.function([flat_tangent, observation, action, atarg], fvp)
    compute_vflossandgrad = tf_util.function([observation, ret], tf_util.flatgrad(vferr, vf_var_list))

    @contextmanager
    def timed(msg):
        if rank == 0:
            print(colorize(msg, color='magenta'))
            start_time = time.time()
            yield
            print(colorize("done in %.3f seconds" % (time.time() - start_time), color='magenta'))
        else:
            yield

    def allmean(arr):
        assert isinstance(arr, np.ndarray)
        out = np.empty_like(arr)
        MPI.COMM_WORLD.Allreduce(arr, out, op=MPI.SUM)
        out /= nworkers
        return out

    tf_util.initialize(sess=sess)

    th_init = get_flat()
    MPI.COMM_WORLD.Bcast(th_init, root=0)
    set_from_flat(th_init)

    if using_gail:
        d_adam.sync()
    vfadam.sync()

    if rank == 0:
        print("Init param sum", th_init.sum(), flush=True)

    # Prepare for rollouts
    # ----------------------------------------
    if using_gail:
        seg_gen = traj_segment_generator(policy, env, timesteps_per_batch, stochastic=True,
                                         reward_giver=reward_giver, gail=True)
    else:
        seg_gen = traj_segment_generator(policy, env, timesteps_per_batch, stochastic=True)

    episodes_so_far = 0
    timesteps_so_far = 0
    iters_so_far = 0
    t_start = time.time()
    lenbuffer = deque(maxlen=40)  # rolling buffer for episode lengths
    rewbuffer = deque(maxlen=40)  # rolling buffer for episode rewards

    assert sum([max_iters > 0, max_timesteps > 0, max_episodes > 0]) == 1

    if using_gail:
        true_rewbuffer = deque(maxlen=40)
        #  Stats not used for now
        #  g_loss_stats = Stats(loss_names)
        #  d_loss_stats = Stats(reward_giver.loss_name)
        #  ep_stats = Stats(["True_rewards", "Rewards", "Episode_length"])

        # if provide pretrained weight
        if pretrained_weight is not None:
            tf_util.load_state(pretrained_weight, var_list=policy.get_variables())

    while True:
        if callback:
            callback(locals(), globals())
        if max_timesteps and timesteps_so_far >= max_timesteps:
            break
        elif max_episodes and episodes_so_far >= max_episodes:
            break
        elif max_iters and iters_so_far >= max_iters:
            break

        # Save model
        if using_gail and rank == 0 and iters_so_far % save_per_iter == 0 and ckpt_dir is not None:
            fname = os.path.join(ckpt_dir, task_name)
            os.makedirs(os.path.dirname(fname), exist_ok=True)
            saver = tf.train.Saver()
            saver.save(sess, fname)

        logger.log("********** Iteration %i ************" % iters_so_far)

        def fisher_vector_product(vec):
            return allmean(compute_fvp(vec, *fvpargs, sess=sess)) + cg_damping * vec
        # ------------------ Update G ------------------
        logger.log("Optimizing Policy...")
        # g_step = 1 when not using GAIL
        for _ in range(g_step):
            with timed("sampling"):
                seg = seg_gen.__next__()
            add_vtarg_and_adv(seg, gamma, lam)
            # ob, ac, atarg, ret, td1ret = map(np.concatenate, (obs, acs, atargs, rets, td1rets))
            observation, action, atarg, tdlamret = seg["ob"], seg["ac"], seg["adv"], seg["tdlamret"]
            vpredbefore = seg["vpred"]  # predicted value function before udpate
            atarg = (atarg - atarg.mean()) / atarg.std()  # standardized advantage function estimate

            if hasattr(policy, "ret_rms"):
                policy.ret_rms.update(tdlamret)
            if hasattr(policy, "ob_rms"):
                policy.ob_rms.update(observation)  # update running mean/std for policy

            args = seg["ob"], seg["ac"], atarg
            fvpargs = [arr[::5] for arr in args]

            assign_old_eq_new(sess=sess)

            with timed("computegrad"):
                *lossbefore, grad = compute_lossandgrad(*args, sess=sess)
            lossbefore = allmean(np.array(lossbefore))
            grad = allmean(grad)
            if np.allclose(grad, 0):
                logger.log("Got zero gradient. not updating")
            else:
                with timed("cg"):
                    stepdir = conjugate_gradient(fisher_vector_product, grad, cg_iters=cg_iters, verbose=rank == 0)
                assert np.isfinite(stepdir).all()
                shs = .5 * stepdir.dot(fisher_vector_product(stepdir))
                # abs(shs) to avoid taking square root of negative values
                lagrange_multiplier = np.sqrt(abs(shs) / max_kl)
                # logger.log("lagrange multiplier:", lm, "gnorm:", np.linalg.norm(g))
                fullstep = stepdir / lagrange_multiplier
                expectedimprove = grad.dot(fullstep)
                surrbefore = lossbefore[0]
                stepsize = 1.0
                thbefore = get_flat()
                for _ in range(10):
                    thnew = thbefore + fullstep * stepsize
                    set_from_flat(thnew)
                    mean_losses = surr, kl_loss, *_ = allmean(np.array(compute_losses(*args, sess=sess)))
                    improve = surr - surrbefore
                    logger.log("Expected: %.3f Actual: %.3f" % (expectedimprove, improve))
                    if not np.isfinite(mean_losses).all():
                        logger.log("Got non-finite value of losses -- bad!")
                    elif kl_loss > max_kl * 1.5:
                        logger.log("violated KL constraint. shrinking step.")
                    elif improve < 0:
                        logger.log("surrogate didn't improve. shrinking step.")
                    else:
                        logger.log("Stepsize OK!")
                        break
                    stepsize *= .5
                else:
                    logger.log("couldn't compute a good step")
                    set_from_flat(thbefore)
                if nworkers > 1 and iters_so_far % 20 == 0:
                    paramsums = MPI.COMM_WORLD.allgather((thnew.sum(), vfadam.getflat().sum()))  # list of tuples
                    assert all(np.allclose(ps, paramsums[0]) for ps in paramsums[1:])

            with timed("vf"):
                for _ in range(vf_iters):
                    for (mbob, mbret) in dataset.iterbatches((seg["ob"], seg["tdlamret"]),
                                                             include_final_partial_batch=False, batch_size=128):
                        if hasattr(policy, "ob_rms"):
                            policy.ob_rms.update(mbob)  # update running mean/std for policy
                        grad = allmean(compute_vflossandgrad(mbob, mbret, sess=sess))
                        vfadam.update(grad, vf_stepsize)

        for (loss_name, loss_val) in zip(loss_names, mean_losses):
            logger.record_tabular(loss_name, loss_val)

        logger.record_tabular("ev_tdlam_before", explained_variance(vpredbefore, tdlamret))

        if using_gail:
            # ------------------ Update D ------------------
            logger.log("Optimizing Discriminator...")
            logger.log(fmt_row(13, reward_giver.loss_name))
            ob_expert, ac_expert = expert_dataset.get_next_batch(len(observation))
            batch_size = len(observation) // d_step
            d_losses = []  # list of tuples, each of which gives the loss for a minibatch
            for ob_batch, ac_batch in dataset.iterbatches((observation, action),
                                                          include_final_partial_batch=False,
                                                          batch_size=batch_size):
                ob_expert, ac_expert = expert_dataset.get_next_batch(len(ob_batch))
                # update running mean/std for reward_giver
                if hasattr(reward_giver, "obs_rms"):
                    reward_giver.obs_rms.update(np.concatenate((ob_batch, ob_expert), 0))
                *newlosses, grad = reward_giver.lossandgrad(ob_batch, ac_batch, ob_expert, ac_expert)
                d_adam.update(allmean(grad), d_stepsize)
                d_losses.append(newlosses)
            logger.log(fmt_row(13, np.mean(d_losses, axis=0)))

            lrlocal = (seg["ep_lens"], seg["ep_rets"], seg["ep_true_rets"])  # local values
            listoflrpairs = MPI.COMM_WORLD.allgather(lrlocal)  # list of tuples
            lens, rews, true_rets = map(flatten_lists, zip(*listoflrpairs))
            true_rewbuffer.extend(true_rets)
        else:
            lrlocal = (seg["ep_lens"], seg["ep_rets"])  # local values
            listoflrpairs = MPI.COMM_WORLD.allgather(lrlocal)  # list of tuples
            lens, rews = map(flatten_lists, zip(*listoflrpairs))
        lenbuffer.extend(lens)
        rewbuffer.extend(rews)

        logger.record_tabular("EpLenMean", np.mean(lenbuffer))
        logger.record_tabular("EpRewMean", np.mean(rewbuffer))
        if using_gail:
            logger.record_tabular("EpTrueRewMean", np.mean(true_rewbuffer))
        logger.record_tabular("EpThisIter", len(lens))
        episodes_so_far += len(lens)
        timesteps_so_far += sum(lens)
        iters_so_far += 1

        logger.record_tabular("EpisodesSoFar", episodes_so_far)
        logger.record_tabular("TimestepsSoFar", timesteps_so_far)
        logger.record_tabular("TimeElapsed", time.time() - t_start)

        if rank == 0:
            logger.dump_tabular()


def flatten_lists(listoflists):
    """
    Flatten a python list of list

    :param listoflists: (list(list))
    :return: (list)
    """
    return [el for list_ in listoflists for el in list_]
