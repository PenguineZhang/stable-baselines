import argparse

import gym
import numpy as np

from baselines import deepq


def callback(lcl, _glb):
    """
    the callback function for logging and saving

    :param lcl: (dict) the local variables
    :param _glb: (dict) the global variables
    :return: (bool) is solved
    """
    # stop training if reward exceeds 199
    if len(lcl['episode_rewards'][-101:-1]) == 0:
        mean_100ep_reward = -np.inf
    else:
        mean_100ep_reward = round(float(np.mean(lcl['episode_rewards'][-101:-1])), 1)
    is_solved = lcl['step'] > 100 and mean_100ep_reward >= 199
    return is_solved


def main(args):
    """
    train and save the DeepQ model, for the cartpole problem

    :param args: (ArgumentParser) the input arguments
    """
    env = gym.make("CartPole-v0")
    model = deepq.models.mlp([64])
    act = deepq.learn(
        env,
        q_func=model,
        learning_rate=1e-3,
        max_timesteps=args.max_timesteps,
        buffer_size=50000,
        exploration_fraction=0.1,
        exploration_final_eps=0.02,
        print_freq=10,
        callback=callback
    )
    print("Saving model to cartpole_model.pkl")
    act.save("cartpole_model.pkl")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Train DQN on cartpole")
    parser.add_argument('--max-timesteps', default=100000, type=int, help="Maximum number of timesteps")
    args = parser.parse_args()
    main(args)
