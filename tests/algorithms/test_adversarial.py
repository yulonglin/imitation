"""Tests for `imitation.algorithms.adversarial.*` algorithms."""

import contextlib
import os
from typing import Any, Mapping

import gym.spaces
import numpy as np
import pytest
import seals  # noqa: F401
import stable_baselines3
import torch as th
from stable_baselines3.common import policies
from torch.utils import data as th_data

from imitation.algorithms.adversarial import airl, common, gail
from imitation.data import rollout, types
from imitation.rewards import reward_nets
from imitation.util import logger, util

ALGORITHM_KWARGS = {
    "airl-ppo": {
        "algorithm_cls": airl.AIRL,
        "model_class": stable_baselines3.PPO,
        "policy_class": policies.ActorCriticPolicy,
    },
    "gail-ppo": {
        "algorithm_cls": gail.GAIL,
        "model_class": stable_baselines3.PPO,
        "policy_class": policies.ActorCriticPolicy,
    },
    "gail-dqn": {
        "algorithm_cls": gail.GAIL,
        "model_class": stable_baselines3.DQN,
        "policy_class": stable_baselines3.dqn.MlpPolicy,
    },
}
IN_CODECOV = "COV_CORE_CONFIG" in os.environ
# Disable SubprocVecEnv tests for code coverage test since
# multiprocessing support is flaky in py.test --cov
PARALLEL = [False] if IN_CODECOV else [True, False]
ENV_NAMES = ["FrozenLake-v1", "CartPole-v1", "Pendulum-v1"]
EXPERT_BATCH_SIZES = [1, 128]


@pytest.fixture(params=ALGORITHM_KWARGS.values(), ids=ALGORITHM_KWARGS.keys())
def _algorithm_kwargs(request):
    """Auto-parametrizes `_rl_algorithm_cls` for the `trainer` fixture."""
    return dict(request.param)


@pytest.fixture
def expert_transitions():
    trajs = types.load("tests/testdata/expert_models/cartpole_0/rollouts/final.pkl")
    trans = rollout.flatten_trajectories(trajs)
    return trans


@contextlib.contextmanager
def make_trainer(
    algorithm_kwargs: Mapping[str, Any],
    tmpdir: str,
    expert_transitions: types.Transitions,
    expert_batch_size: int = 1,
    env_name: str = "seals/CartPole-v0",
    num_envs: int = 1,
    parallel: bool = False,
    convert_dataset: bool = False,
):
    if convert_dataset:
        expert_data = th_data.DataLoader(
            expert_transitions,
            batch_size=expert_batch_size,
            collate_fn=types.transitions_collate_fn,
            shuffle=True,
            drop_last=True,
        )
    else:
        expert_data = expert_transitions

    venv = util.make_vec_env(env_name, n_envs=num_envs, parallel=parallel)
    model_cls = algorithm_kwargs["model_class"]
    gen_algo = model_cls(algorithm_kwargs["policy_class"], venv)
    reward_net_cls = reward_nets.BasicRewardNet
    if algorithm_kwargs["algorithm_cls"] == airl.AIRL:
        reward_net_cls = reward_nets.BasicShapedRewardNet
    reward_net = reward_net_cls(venv.observation_space, venv.action_space)
    custom_logger = logger.configure(tmpdir, ["tensorboard", "stdout"])

    normalize = isinstance(venv.observation_space, gym.spaces.Box)
    trainer = algorithm_kwargs["algorithm_cls"](
        venv=venv,
        # TODO(adam): remove following line when SB3 PR merged:
        # https://github.com/DLR-RM/stable-baselines3/pull/575
        normalize_reward=normalize,
        demonstrations=expert_data,
        demo_batch_size=expert_batch_size,
        gen_algo=gen_algo,
        reward_net=reward_net,
        log_dir=tmpdir,
        custom_logger=custom_logger,
    )

    try:
        yield trainer
    finally:
        venv.close()


def test_airl_fail_fast(custom_logger, tmpdir):
    venv = util.make_vec_env(
        "seals/CartPole-v0",
        n_envs=1,
        parallel=False,
    )

    gen_algo = stable_baselines3.DQN(stable_baselines3.dqn.MlpPolicy, venv)
    small_data = rollout.generate_transitions(gen_algo, venv, n_timesteps=20)
    reward_net = reward_nets.BasicShapedRewardNet(
        observation_space=venv.observation_space,
        action_space=venv.action_space,
    )

    with pytest.raises(TypeError, match="AIRL needs a stochastic policy.*"):
        airl.AIRL(
            venv=venv,
            demonstrations=small_data,
            demo_batch_size=20,
            gen_algo=gen_algo,
            reward_net=reward_net,
            log_dir=tmpdir,
            custom_logger=custom_logger,
        )


@pytest.fixture(params=ALGORITHM_KWARGS.values(), ids=ALGORITHM_KWARGS.keys())
def trainer(request, tmpdir, expert_transitions):
    with make_trainer(request.param, tmpdir, expert_transitions) as trainer:
        yield trainer


def test_train_disc_no_samples_error(trainer: common.AdversarialTrainer):
    with pytest.raises(RuntimeError, match="No generator samples"):
        trainer.train_disc()


def test_train_disc_unequal_expert_gen_samples_error(
    trainer: common.AdversarialTrainer,
    expert_transitions: types.Transitions,
):
    """Test that train_disc raises error when n_gen != n_expert samples."""
    if len(expert_transitions) < 2:  # pragma: no cover
        raise ValueError("Test assumes at least 2 samples.")
    expert_samples = types.dataclass_quick_asdict(expert_transitions)
    gen_samples = types.dataclass_quick_asdict(expert_transitions[:-1])
    with pytest.raises(ValueError, match="n_expert"):
        trainer.train_disc(expert_samples=expert_samples, gen_samples=gen_samples)


@pytest.fixture(params=PARALLEL)
def _parallel(request):
    """Auto-parametrizes `_parallel`."""
    return request.param


@pytest.fixture(params=[False, True])
def _convert_dataset(request):
    """Auto-parametrizes `_parallel`."""
    return request.param


@pytest.fixture(params=[1, 128])
def _expert_batch_size(request):
    """Auto-parameterizes `_expert_batch_size`."""
    return request.param


@pytest.fixture
def trainer_parametrized(
    _algorithm_kwargs,
    _parallel,
    _convert_dataset,
    _expert_batch_size,
    tmpdir,
    expert_transitions,
):
    with make_trainer(
        _algorithm_kwargs,
        tmpdir,
        expert_transitions,
        parallel=_parallel,
        convert_dataset=_convert_dataset,
        expert_batch_size=_expert_batch_size,
    ) as trainer:
        yield trainer


def test_train_disc_step_no_crash(trainer_parametrized, _expert_batch_size):
    transitions = rollout.generate_transitions(
        trainer_parametrized.gen_algo,
        trainer_parametrized.venv,
        n_timesteps=_expert_batch_size,
        truncate=True,
    )
    trainer_parametrized.train_disc(
        gen_samples=types.dataclass_quick_asdict(transitions),
    )


def test_train_gen_train_disc_no_crash(
    trainer_parametrized: common.AdversarialTrainer,
    n_updates: int = 2,
) -> None:
    trainer_parametrized.train_gen(n_updates * trainer_parametrized.gen_train_timesteps)
    trainer_parametrized.train_disc()


@pytest.fixture
def trainer_batch_sizes(
    _algorithm_kwargs,
    _expert_batch_size,
    tmpdir,
    expert_transitions,
):
    with make_trainer(
        _algorithm_kwargs,
        tmpdir,
        expert_transitions,
        expert_batch_size=_expert_batch_size,
    ) as trainer:
        yield trainer


def test_train_disc_improve_D(
    trainer_batch_sizes,
    tmpdir,
    expert_transitions,
    _expert_batch_size,
    n_steps=3,
):
    expert_samples = expert_transitions[:_expert_batch_size]
    expert_samples = types.dataclass_quick_asdict(expert_samples)
    gen_samples = rollout.generate_transitions(
        trainer_batch_sizes.gen_algo,
        trainer_batch_sizes.venv_train,
        n_timesteps=_expert_batch_size,
        truncate=True,
    )
    gen_samples = types.dataclass_quick_asdict(gen_samples)
    init_stats = final_stats = None
    for _ in range(n_steps):
        final_stats = trainer_batch_sizes.train_disc(
            gen_samples=gen_samples,
            expert_samples=expert_samples,
        )
        if init_stats is None:
            init_stats = final_stats
    assert final_stats["disc_loss"] < init_stats["disc_loss"]


@pytest.fixture(params=ENV_NAMES)
def _env_name(request):
    """Auto-parameterizes `_env_name`."""
    return request.param


@pytest.fixture
def trainer_diverse_env(_algorithm_kwargs, _env_name, tmpdir, expert_transitions):
    if _algorithm_kwargs["model_class"] == stable_baselines3.DQN:
        pytest.skip("DQN does not support all environments.")
    with make_trainer(
        _algorithm_kwargs,
        tmpdir,
        expert_transitions,
        env_name=_env_name,
    ) as trainer:
        yield trainer


@pytest.mark.parametrize("n_timesteps", [2, 4, 10])
def test_logits_gen_is_high_log_policy_act_prob(
    trainer_diverse_env: common.AdversarialTrainer,
    n_timesteps: int,
):
    """Smoke test calling `logits_gen_is_high` on `AdversarialTrainer`.

    For AIRL, also checks that the function raises
    error on `log_policy_act_prob=None`.

    Args:
        trainer_diverse_env: The trainer to test.
        n_timesteps: The number of timesteps of rollouts to collect.
    """
    trans = rollout.generate_transitions(
        policy=None,
        venv=trainer_diverse_env.venv,
        n_timesteps=n_timesteps,
    )

    obs, acts, next_obs, dones = trainer_diverse_env.reward_train.preprocess(
        trans.obs,
        trans.acts,
        trans.next_obs,
        trans.dones,
    )
    log_act_prob_non_none = np.log(0.1 + 0.9 * np.random.rand(n_timesteps))
    log_act_prob_non_none = th.as_tensor(log_act_prob_non_none).to(obs.device)

    for log_act_prob in [None, log_act_prob_non_none]:
        if isinstance(trainer_diverse_env, airl.AIRL) and log_act_prob is None:
            maybe_error_ctx = pytest.raises(TypeError, match="Non-None.*required.*")
        else:
            maybe_error_ctx = contextlib.nullcontext()

        with maybe_error_ctx:
            trainer_diverse_env.logits_gen_is_high(
                obs,
                acts,
                next_obs,
                dones,
                log_act_prob,
            )


@pytest.mark.parametrize("n_samples", [0, 1, 10, 40])
def test_compute_train_stats(n_samples):
    disc_logits_gen_is_high = th.from_numpy(np.random.standard_normal([n_samples]) * 10)
    labels_gen_is_one = th.from_numpy(np.random.randint(2, size=[n_samples]))
    disc_loss = th.tensor(np.random.random() * 10)
    stats = common.compute_train_stats(
        disc_logits_gen_is_high,
        labels_gen_is_one,
        disc_loss,
    )
    for k, v in stats.items():
        assert isinstance(k, str)
        assert isinstance(v, float)
