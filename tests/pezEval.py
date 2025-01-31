import multiprocessing
import sys
from pettingzoo.test import parallel_api_test

import multiprocessing
import sys
from pettingzoo.test import parallel_api_test
from citylearn import GridLearn
from citylearn import MyEnv
from pathlib import Path
from stable_baselines3.ppo import MlpPolicy
from stable_baselines3 import PPO
import gym
import numpy as np
from copy import deepcopy
import multiprocessing
import sys
import supersuit as ss
import time
import os
import random
random.seed(12)
np.random.seed(12)
# multiprocessing.set_start_method("fork")

model_name = "null_batt"
climate_zone = 1
data_path = Path("../citylearn/data/Climate_Zone_"+str(climate_zone))
buildings_states_actions = '../citylearn/buildings_state_action_space.json'

config = {
    "model_name":model_name,
    "data_path":data_path,
    "climate_zone":climate_zone,
    "buildings_states_actions_file":buildings_states_actions,
    "hourly_timesteps":4,
    "percent_rl":0.5,
    # "percent_rl":1,
    "nclusters":1,
    "max_num_houses":None
    # "max_num_houses":4
}

grid = GridLearn(**config)

env = MyEnv(grid) #for _ in range(config['nclusters'])
env.grid = grid
env.initialize_rbc_agents()

print('creating pettingzoo env...')
env = ss.pettingzoo_env_to_vec_env_v0(env)

print('stacking vec env...')
# nenvs = 2
env = ss.concat_vec_envs_v0(env, 1, num_cpus=1, base_class='stable_baselines3')

grid.normalize_reward()
# print('setting the grid...')
# for env in envs:
#     for n in range(nenvs):
#         # env.venv.vec_envs[n].par_env.aec_env.env.env.env.grid = grids[n]
#         env.venv.vec_envs[n].par_env.grid = grids[n]
#         # env.venv.vec_envs[n].par_env.aec_env.env.env.env.initialize_rbc_agents()
#         env.venv.vec_envs[n].par_env.initialize_rbc_agents()

# models = [PPO.load(f"models/{model_name}/model_{m}") for m in range(len(envs))]
model = PPO.load(f"models/{model_name}/model")

sum_reward = 0
obss env.reset() #for env in envs]
for ts in range(365*24*4): # test on 5 timesteps
    # for m in range(len(models)): # again, alternate through models
        # # get the current observation from the perspective of the active team
        # # this can probably be cleaned up
        # foo = []
        # for e in range(nenvs):
        #     bar = list(envs[m].venv.vec_envs[n].par_env.aec_env.env.env.env.env.state().values())
        #     for i in range(len(bar)):
        #         while len(bar[i]) < 13:
        #             bar[i] = np.append(bar[i], 0)
        #     foo += bar
        #
        # obss[m] = np.vstack(foo)

    action = model.predict(obss, deterministic=True)[0] # send it to the SB model to select an action
    print(action)
    print(grid.ts)
    obss, reward, done, info = env.step(action) # update environment

env.venv.vec_envs[0].par_env.grid.plot_all()
