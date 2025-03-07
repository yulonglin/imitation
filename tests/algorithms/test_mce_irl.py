"""Test `imitation.algorithms.tabular_irl` and tabular environments."""

from typing import Any, Mapping

import gym
import numpy as np
import pytest
import torch as th
from stable_baselines3.common import vec_env

from imitation.algorithms import base
from imitation.algorithms.mce_irl import (
    MCEIRL,
    TabularPolicy,
    mce_occupancy_measures,
    mce_partition_fh,
)
from imitation.data import rollout
from imitation.envs import resettable_env
from imitation.envs.examples import model_envs
from imitation.rewards import reward_nets
from imitation.util.util import tensor_iter_norm


def rollouts(env, n=10, seed=None):
    rv = []
    for i in range(n):
        done = False
        if seed is not None:
            # if a seed is given, then we use the same seed each time (should
            # give same trajectory each time)
            env.seed(seed)
            env.action_space.seed(seed)
        obs = env.reset()
        traj = [obs]
        while not done:
            act = env.action_space.sample()
            obs, rew, done, info = env.step(act)
            traj.append((obs, rew))
        rv.append(traj)
    return rv


def test_random_mdp():
    for i in range(3):
        n_states = 4 * (i + 3)
        n_actions = i + 2
        branch_factor = i + 1
        if branch_factor == 1:
            # make sure we have enough actions to get reasonable trajectories
            n_actions = min(n_states, max(n_actions, 4))
        horizon = 5 * (i + 1)
        random_obs = (i % 2) == 0
        obs_dim = (i * 3 + 4) ** 2 + i
        mdp = model_envs.RandomMDP(
            n_states=n_states,
            n_actions=n_actions,
            branch_factor=branch_factor,
            horizon=horizon,
            random_obs=random_obs,
            obs_dim=obs_dim if random_obs else None,
            generator_seed=i,
        )

        # sanity checks on sizes of things
        assert mdp.transition_matrix.shape == (n_states, n_actions, n_states)
        assert np.allclose(1, np.sum(mdp.transition_matrix, axis=-1))
        assert np.all(mdp.transition_matrix >= 0)
        assert (
            mdp.observation_matrix.shape[0] == n_states
            and mdp.observation_matrix.ndim == 2
        )
        assert mdp.reward_matrix.shape == (n_states,)
        assert mdp.horizon == horizon
        assert np.all(mdp.initial_state_dist >= 0)
        assert np.allclose(1, np.sum(mdp.initial_state_dist))
        assert np.sum(mdp.initial_state_dist > 0) == branch_factor

        # make sure trajectories aren't all the same if we don't specify same
        # seed each time
        trajectories = rollouts(mdp, 100)
        assert len(set(map(str, trajectories))) > 1

        trajectories = rollouts(mdp, 100, seed=42)
        # make sure trajectories ARE all the same if we do specify the same
        # seed each time
        assert len(set(map(str, trajectories))) == 1


FEW_DISCOUNT_RATES = [0.0, 0.99, 1.0]
DISCOUNT_RATES = FEW_DISCOUNT_RATES + [0.5, 0.9]


@pytest.mark.parametrize("discount", DISCOUNT_RATES)
def test_policy_om_random_mdp(discount: float):
    """Test that optimal policy occupancy measure ("om") for a random MDP is sane."""
    mdp = gym.make("imitation/Random-v0")
    V, Q, pi = mce_partition_fh(mdp, discount=discount)
    assert np.all(np.isfinite(V))
    assert np.all(np.isfinite(Q))
    assert np.all(np.isfinite(pi))
    # Check it is a probability distribution along the last axis
    assert np.all(pi >= 0)
    assert np.allclose(np.sum(pi, axis=-1), 1)

    Dt, D = mce_occupancy_measures(mdp, pi=pi, discount=discount)
    assert np.all(np.isfinite(D))
    assert np.any(D > 0)
    # expected number of state visits (over all states) should be equal to the
    # horizon
    if discount == 1.0:
        expected_sum = mdp.horizon
    else:
        expected_sum = (1 - discount**mdp.horizon) / (1 - discount)
    assert np.allclose(np.sum(D), expected_sum)


class ReasonableMDP(resettable_env.TabularModelEnv):
    """A tabular MDP with sensible parameters."""

    observation_matrix = np.array(
        [
            [3, -5, -1, -1, -4, 5, 3, 0],
            # state 1 (top)
            [4, -4, 2, 2, -4, -1, -2, -2],
            # state 2 (bottom, equiv to top)
            [3, -1, 5, -1, 0, 2, -5, 2],
            # state 3 (middle, very low reward and so dominated by others)
            [-5, -1, 4, 1, 4, 1, 5, 3],
            # state 4 (final, all self loops, good reward)
            [2, -5, 1, -5, 1, 4, 4, -3],
        ],
    )
    transition_matrix = np.array(
        [
            # transitions out of state 0
            [
                # action 0: goes to state 1 (sometimes 2)
                [0, 0.9, 0.1, 0, 0],
                # action 1: goes to state 3 deterministically
                [0, 0, 0, 1, 0],
                # action 2: goes to state 2 (sometimes 2)
                [0, 0.1, 0.9, 0, 0],
            ],
            # transitions out of state 1
            [
                # action 0: goes to state 3 or 4 (sub-optimal)
                [0, 0, 0, 0.05, 0.95],
                # action 1: goes to state 3 (bad)
                [0, 0, 0, 1, 0],
                # action 2: goes to state 4 (good!)
                [0, 0, 0, 0, 1],
            ],
            # transitions out of state 2 (basically the same)
            [
                # action 0: goes to state 3 or 4 (sub-optimal)
                [0, 0, 0, 0.05, 0.95],
                # action 1: goes to state 3 (bad)
                [0, 0, 0, 1, 0],
                # action 2: goes to state 4 (good!)
                [0, 0, 0, 0, 1],
            ],
            # transitions out of state 3 (all go to state 4)
            [
                # action 0
                [0, 0, 0, 0, 1],
                # action 1
                [0, 0, 0, 0, 1],
                # action 2
                [0, 0, 0, 0, 1],
            ],
            # transitions out of state 4 (all go back to state 0)
            [
                # action 0
                [1, 0, 0, 0, 0],
                # action 1
                [1, 0, 0, 0, 0],
                # action 2
                [1, 0, 0, 0, 0],
            ],
        ],
    )
    reward_matrix = np.array(
        [
            # state 0 (okay reward, but we can't go back so it doesn't matter)
            1,
            # states 1 & 2 have same (okay) reward
            2,
            2,
            # state 3 has very negative reward (so avoid it!)
            -20,
            # state 4 has pretty good reward (good enough that we should move out
            # of 1 & 2)
            3,
        ],
    )
    # always start in s0 or s4
    initial_state_dist = [0.5, 0, 0, 0, 0.5]
    horizon = 20


@pytest.mark.parametrize("discount", DISCOUNT_RATES)
def test_policy_om_reasonable_mdp(discount: float):
    # MDP described above
    mdp = ReasonableMDP()
    # get policy etc. for our MDP
    V, Q, pi = mce_partition_fh(mdp, discount=discount)
    Dt, D = mce_occupancy_measures(mdp, pi=pi, discount=discount)
    assert np.all(np.isfinite(V))
    assert np.all(np.isfinite(Q))
    assert np.all(np.isfinite(pi))
    assert np.all(np.isfinite(Dt))
    assert np.all(np.isfinite(D))
    # check that actions 0 & 2 (which go to states 1 & 2) are roughly equal
    assert np.allclose(pi[:19, 0, 0], pi[:19, 0, 2])
    # also check that they're by far preferred to action 1 (that goes to state
    # 3, which has poor reward)
    if discount > 0:
        assert np.all(pi[:19, 0, 0] > 2 * pi[:19, 0, 1])
    # make sure that states 3 & 4 have roughly uniform policies
    pi_34 = pi[:5, 3:5]
    assert np.allclose(pi_34, np.ones_like(pi_34) / 3.0)
    # check that states 1 & 2 have similar policies to each other
    assert np.allclose(pi[:19, 1, :], pi[:19, 2, :])
    # check that in state 1, action 2 (which goes to state 4 with certainty) is
    # better than action 0 (which only gets there with some probability), and
    # that both are better than action 1 (which always goes to the bad state).
    if discount > 0:
        assert np.all(pi[:19, 1, 2] > pi[:19, 1, 0])
        assert np.all(pi[:19, 1, 0] > pi[:19, 1, 1])
    # check that Dt[0] matches our initial state dist
    assert np.allclose(Dt[0], mdp.initial_state_dist)


def test_tabular_policy():
    """Tests tabular policy prediction, especially timestep calculation and masking."""
    state_space = gym.spaces.Discrete(2)
    action_space = gym.spaces.Discrete(2)
    pi = np.stack(
        [np.eye(2), 1 - np.eye(2)],
    )
    rng = np.random.RandomState(42)
    tabular = TabularPolicy(
        state_space=state_space,
        action_space=action_space,
        pi=pi,
        rng=rng,
    )

    states = np.array([0, 1, 1, 0, 1])
    actions, timesteps = tabular.predict(states)
    np.testing.assert_array_equal(states, actions)
    np.testing.assert_equal(timesteps, 1)

    mask = np.zeros((5,), dtype=bool)
    actions, timesteps = tabular.predict(states, timesteps, mask)
    np.testing.assert_array_equal(1 - states, actions)
    np.testing.assert_equal(timesteps, 2)

    mask = np.ones((5,), dtype=bool)
    actions, timesteps = tabular.predict(states, timesteps, mask)
    np.testing.assert_array_equal(states, actions)
    np.testing.assert_equal(timesteps, 1)

    mask = (1 - states).astype(bool)
    actions, timesteps = tabular.predict(states, timesteps, mask)
    np.testing.assert_array_equal(np.zeros((5,)), actions)
    np.testing.assert_equal(timesteps, 2 - mask.astype(int))


def test_tabular_policy_randomness():
    state_space = gym.spaces.Discrete(2)
    action_space = gym.spaces.Discrete(2)
    pi = np.array(
        [
            [
                [0.5, 0.5],
                [0.9, 0.1],
            ],
        ],
    )
    rng = np.random.RandomState(42)
    tabular = TabularPolicy(
        state_space=state_space,
        action_space=action_space,
        pi=pi,
        rng=rng,
    )

    actions, _ = tabular.predict(np.zeros((100,), dtype=int))
    assert 0.45 <= np.mean(actions) <= 0.55
    ones_obs = np.ones((100,), dtype=int)
    actions, _ = tabular.predict(ones_obs)
    assert 0.05 <= np.mean(actions) <= 0.15
    actions, _ = tabular.predict(ones_obs, deterministic=True)
    np.testing.assert_equal(actions, 0)


def test_mce_irl_demo_formats():
    mdp = model_envs.RandomMDP(
        n_states=5,
        n_actions=3,
        branch_factor=2,
        horizon=10,
        random_obs=False,
        obs_dim=None,
        generator_seed=42,
    )
    venv = vec_env.DummyVecEnv([lambda: mdp])
    state_venv = resettable_env.DictExtractWrapper(venv, "state")
    trajs = rollout.generate_trajectories(
        policy=None,
        venv=state_venv,
        sample_until=rollout.make_min_timesteps(100),
    )
    demonstrations = {
        "trajs": trajs,
        "trans": rollout.flatten_trajectories(trajs),
        "data_loader": base.make_data_loader(trajs, batch_size=32),
    }

    final_counts = {}
    for kind, demo in demonstrations.items():
        with th.random.fork_rng():
            th.random.manual_seed(715298)
            # create reward network so we can be sure it's seeded identically
            reward_net = reward_nets.BasicRewardNet(
                mdp.pomdp_observation_space,
                mdp.action_space,
                use_action=False,
                use_next_state=False,
                use_done=False,
                hid_sizes=[],
            )
            mce_irl = MCEIRL(demo, mdp, reward_net, linf_eps=1e-3)
            assert np.allclose(mce_irl.demo_state_om.sum(), 1.0)
            final_counts[kind] = mce_irl.train(max_iter=5)

            # make sure weights have non-insane norm
            assert tensor_iter_norm(mce_irl.reward_net.parameters()) < 1000

    for cts in final_counts.values():
        assert np.allclose(cts, final_counts["trajs"], atol=1e-3, rtol=1e-3)


@pytest.mark.expensive
@pytest.mark.parametrize(
    "model_kwargs",
    [dict(hid_sizes=[]), dict(hid_sizes=[32, 32])],
)
@pytest.mark.parametrize("discount", FEW_DISCOUNT_RATES)
def test_mce_irl_reasonable_mdp(
    model_kwargs: Mapping[str, Any],
    discount: float,
):
    with th.random.fork_rng():
        th.random.manual_seed(715298)

        # test MCE IRL on the MDP
        mdp = ReasonableMDP()
        mdp.seed(715298)

        # demo occupancy measure
        V, Q, pi = mce_partition_fh(mdp, discount=discount)
        Dt, D = mce_occupancy_measures(mdp, pi=pi, discount=discount)

        reward_net = reward_nets.BasicRewardNet(
            mdp.pomdp_observation_space,
            mdp.action_space,
            use_action=False,
            use_next_state=False,
            use_done=False,
            **model_kwargs,
        )
        mce_irl = MCEIRL(D, mdp, reward_net, linf_eps=1e-3, discount=discount)
        final_counts = mce_irl.train()

        assert np.allclose(final_counts, D, atol=1e-3, rtol=1e-3)
        # make sure weights have non-insane norm
        assert tensor_iter_norm(reward_net.parameters()) < 1000

        venv = vec_env.DummyVecEnv([lambda: mdp])
        state_venv = resettable_env.DictExtractWrapper(venv, "state")
        trajs = rollout.generate_trajectories(
            mce_irl.policy,
            state_venv,
            sample_until=rollout.make_min_episodes(5),
        )
        stats = rollout.rollout_stats(trajs)
        if discount > 0.0:  # skip check when discount==0.0 (random policy)
            assert stats["return_mean"] >= mdp.horizon * 2 * 0.8
