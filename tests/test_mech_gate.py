"""Weight setting must be gated on THIS mechanism's LastUpdate.

SN93 carries two mechanisms on one metagraph, and both run on the same
hotkey and UID. ``metagraph.last_update`` only ever reflects mechanism 0, so
gating on it lets a co-located YouTube validator hold an X validator's gate
permanently shut.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from bittensor.utils import get_mechid_storage_index

from bitcast.base.neuron import BaseNeuron

NETUID = 93


class _StubNeuron(BaseNeuron):
    """Concrete BaseNeuron: the abstract methods are irrelevant to the gate."""

    _block = 0

    def forward(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError

    def run(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError

    @property
    def block(self):
        return self._block


def make_neuron(block, metagraph_last_update, mech_last_update, mechid=1, epoch_length=100):
    """A neuron whose metagraph counter and mechanism counter deliberately differ."""
    neuron = object.__new__(_StubNeuron)
    neuron._block = block
    neuron.step = 5
    neuron.uid = 1
    neuron.neuron_type = "ValidatorNeuron"
    neuron.metagraph = SimpleNamespace(last_update=np.full(4, metagraph_last_update))
    neuron.config = SimpleNamespace(
        netuid=NETUID,
        neuron=SimpleNamespace(epoch_length=epoch_length, disable_set_weights=False),
    )

    index = get_mechid_storage_index(NETUID, mechid)

    class FakeSubstrate:
        def __init__(self):
            self.queried = []

        def query(self, module, name, params):
            assert (module, name) == ("SubtensorModule", "LastUpdate")
            self.queried.append(params[0])
            if params[0] != index:
                raise AssertionError(f"queried storage index {params[0]}, expected {index}")
            return SimpleNamespace(value=[mech_last_update] * 4)

    neuron.subtensor = SimpleNamespace(substrate=FakeSubstrate())
    return neuron


@pytest.fixture(autouse=True)
def _mechid_env():
    with patch.dict(os.environ, {"MECHID": "1"}):
        yield


def test_fires_when_own_mechanism_is_stale_but_metagraph_is_fresh():
    """The regression, with the live numbers that exposed it: mechanism 0 was
    81 blocks old because the co-located YouTube validator had just set
    weights, while this validator's own mechanism had never been written."""
    neuron = make_neuron(
        block=8773068, metagraph_last_update=8772987, mech_last_update=8656644
    )
    assert neuron.should_set_weights() is True


def test_does_not_fire_when_own_mechanism_is_fresh():
    neuron = make_neuron(block=1000, metagraph_last_update=0, mech_last_update=950)
    assert neuron.should_set_weights() is False


def test_queries_the_mechid_storage_index_not_the_netuid_slot():
    neuron = make_neuron(block=1000, metagraph_last_update=0, mech_last_update=800)
    neuron.should_set_weights()
    assert neuron.subtensor.substrate.queried == [get_mechid_storage_index(NETUID, 1)]
    assert neuron.subtensor.substrate.queried[0] != NETUID


def test_rpc_failure_falls_back_to_metagraph_and_stays_conservative():
    neuron = make_neuron(block=1000, metagraph_last_update=950, mech_last_update=0)

    def boom(module, name, params):
        raise RuntimeError("rpc down")

    neuron.subtensor.substrate.query = boom
    # Metagraph says 50 blocks elapsed, under epoch_length — gate stays shut.
    assert neuron.should_set_weights() is False


def test_miner_never_sets_weights():
    neuron = make_neuron(block=1000, metagraph_last_update=0, mech_last_update=0)
    neuron.neuron_type = "MinerNeuron"
    assert neuron.should_set_weights() is False


def test_step_zero_never_sets_weights():
    neuron = make_neuron(block=1000, metagraph_last_update=0, mech_last_update=0)
    neuron.step = 0
    assert neuron.should_set_weights() is False
