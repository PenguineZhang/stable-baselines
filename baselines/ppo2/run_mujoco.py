#!/usr/bin/env python3
import numpy as np
import gym

from baselines.common.cmd_util import mujoco_arg_parser
from baselines import bench, logger
from baselines.common import set_global_seeds
from baselines.common.vec_env.vec_normalize import VecNormalize
from baselines.ppo2 import ppo2
from baselines.a2c.policies import MlpPolicy
from baselines.common.vec_env.dummy_vec_env import DummyVecEnv


def train(env_id, num_timesteps, seed):
    """
    Train PPO2 model for Mujoco environment, for testing purposes

    :param env_id: (str) the environment id string
    :param num_timesteps: (int) the number of timesteps to run
    :param seed: (int) Used to seed the random generator.
    """
    def make_env():
        env_out = gym.make(env_id)
        env_out = bench.Monitor(env_out, logger.get_dir(), allow_early_resets=True)
        return env_out

    env = DummyVecEnv([make_env])
    env = VecNormalize(env)

    set_global_seeds(seed)
    policy = MlpPolicy
    model = ppo2.learn(policy=policy, env=env, n_steps=2048, nminibatches=32,
                       lam=0.95, gamma=0.99, noptepochs=10, log_interval=1,
                       ent_coef=0.0,
                       learning_rate=3e-4,
                       cliprange=0.2,
                       total_timesteps=num_timesteps)

    return model, env


def main():
    """
    Runs the test
    """
    args = mujoco_arg_parser().parse_args()
    logger.configure()
    model, env = train(args.env, num_timesteps=args.num_timesteps, seed=args.seed)

    if args.play:
        logger.log("Running trained model")
        obs = np.zeros((env.num_envs,) + env.observation_space.shape)
        obs[:] = env.reset()
        while True:
            actions = model.step(obs)[0]
            obs[:] = env.step(actions)[0]
            env.render()


if __name__ == '__main__':
    main()
