from collections.abc import Callable, Iterable
from copy import deepcopy
import random
from threading import Lock, Thread
from types import MethodType
from typing import Optional

import torch
from torch import nn, Tensor
from torch.utils.data import DataLoader
from torch.utils.hooks import RemovableHandle

import slayerSNN as snn
from slayerSNN.slayer import spikeLayer
from slayerSNN.utils import stats as spikeStats

from .fault import Fault, FaultModel, FaultRound, FaultSite, FaultTarget, OptimizedFaultRound
from .utils.layer import LayersInfo
from .utils.progress import CampaignProgress, refresh_progress_job


# TODO: Fix long lines
# TODO: Logging (in methods that might take a long time based on the rounds number, e.g., in _pre_run)
# TODO: Results manipulation (read/write results in io.py and save/load results here)
# TODO: Verify results validity
# TODO: Parallelize fault rounds evaluation (?)


class Campaign:
    def __init__(self, net: nn.Module, shape_in: tuple[int, int, int], slayer: spikeLayer) -> None:
        self.golden = net
        self.golden.eval()
        self.faulty = None
        self.slayer = slayer
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.layers_info = LayersInfo()
        self.infer_layers_info(shape_in)

        # Assign optimized forward function to golden network
        self.golden.forward = MethodType(Campaign._forward_opt_wrapper(self.layers_info, self.slayer), self.golden)

        self.r_idx = 0
        self.rounds: list[FaultRound] = [FaultRound()]
        self.orounds: list[OptimizedFaultRound] = []
        self.rgroups: dict[str, list[int]] = {}
        self.handles: dict[str, list[list[RemovableHandle]]] = {}
        self.performance: list[spikeStats] = []

    def __repr__(self) -> str:
        s = 'FI Campaign:\n'
        s += f"  - Network: '{self.golden.__class__.__name__}':\n"
        s += f"  - {str(self.layers_info).replace('}', '  }')}\n"
        s += f"  - Rounds ({len(self.rounds)}): {{\n"
        for round_idx, round in enumerate(self.rounds):
            round_str = str(round).replace('\n', '\n      ')
            s += f"      #{round_idx}: {round_str}\n"
        s += '  }'

        return s

    def infer_layers_info(self, shape_in: tuple[int, int, int]) -> None:
        handles = []
        for name, child in self.golden.named_children():
            hook = self.layers_info.infer_hook_wrapper(name)
            handle = child.register_forward_hook(hook)
            handles.append(handle)

        dummy_input = torch.rand((1, *shape_in, 1)).to(self.device)
        self.golden(dummy_input)

        for handle in handles:
            handle.remove()

    def inject(self, faults: Iterable[Fault], round_idx: int = -1) -> list[Fault]:
        assert -len(self.rounds) <= round_idx < len(self.rounds), f'Invalid round index {round_idx}'

        self.define_random(faults)
        inj_faults = self.validate(faults)

        self.rounds[round_idx].insert_many(inj_faults)

        return inj_faults

    def define_random(self, faults: Iterable[Fault]) -> Iterable[Fault]:
        if not isinstance(faults, Iterable):
            raise TypeError(f"'{type(faults).__name__}' object is not iterable")

        has_site_duplicates = False
        for f in faults:
            is_syn = f.model.is_synaptic()

            for s in f.sites_pending:
                if not s.layer:
                    s.layer = random.choice(self.layers_info.get_injectables())

                shape = self.layers_info.get_shapes(is_syn, s.layer)

                pos = list(s.position)
                if pos[0] is None:
                    pos[0] = random.randrange(shape[0]) if is_syn else slice(None)

                for i in range(1, 4):
                    if pos[i] is None:
                        si = i - (not is_syn)
                        pos[i] = random.randrange(shape[si])

                s.position = tuple(pos)

            f.refresh(discard_duplicates=False)
            has_site_duplicates |= bool(f.sites_pending)

        if has_site_duplicates:
            print('Some of the newly defined random fault sites already exist.')

        return faults

    def validate(self, faults: Iterable[Fault]) -> list[Fault]:
        if not isinstance(faults, Iterable):
            raise TypeError(f"'{type(faults).__name__}' object is not iterable")

        valid_faults = []
        for f in faults:
            if f.model.is_parametric() and f.model.param_name not in self.slayer.neuron:
                continue

            is_syn = f.model.is_synaptic()
            to_remove = set()

            for s in f.sites:  # Validate only the defined fault sites
                v = self.layers_info.is_injectable(s.layer)
                if v:
                    shape = self.layers_info.get_shapes(is_syn, s.layer)
                    if is_syn:
                        v &= -shape[0] <= s.position[0] < shape[0]

                    for i in range(1, 4):
                        # shapes_neu index values, si: 0-2
                        # shapes_syn index values, si: 1-3
                        si = i - (not is_syn)
                        v &= -shape[si] <= s.position[i] < shape[si]

                if not v:
                    to_remove.add(s)

            f.sites.difference_update(to_remove)
            if f:
                valid_faults.append(f)

        return valid_faults

    def then_inject(self, faults: Iterable[Fault]) -> list[Fault]:
        self.rounds.append(FaultRound())
        return self.inject(faults, -1)

    def eject(self, faults: Iterable[Fault] = None, round_idx: int = None) -> None:
        if faults is not None and not isinstance(faults, Iterable):
            raise TypeError(f"'{type(faults).__name__}' object is not iterable")

        # Eject from a specific round
        if round_idx:
            # Eject indicated faults from the round
            if faults:
                self.rounds[round_idx].extract_many(faults)
            # Eject all faults from the round, i.e., remove the round itself
            if not faults or not self.rounds[round_idx]:
                self.rounds.pop(round_idx)
        # Eject from all rounds
        else:
            # Eject indicated faults from any round the might exist
            if faults:
                for r in self.rounds:
                    r.extract_many(faults)
                    if not r:
                        self.rounds.pop(r)
            # Eject all faults from all rounds, i.e., all the rounds themselves
            else:
                self.rounds.clear()

        if not self.rounds:
            self.rounds.append(FaultRound())

    def run(self, test_loader: DataLoader, error: snn.loss = None) -> None:
        self._pre_run()

        # Initialize and refresh progress
        self.progress = CampaignProgress(len(test_loader), len(self.rounds))
        self.progress_lock = Lock()
        progress_thread = Thread(target=refresh_progress_job, args=(self.progress, .1,), daemon=True)
        progress_thread.start()

        # Evaluate faults' effects
        with torch.no_grad():
            if len(self.rounds) <= 1:
                self._evaluate_single(test_loader, error)
            else:
                self._evaluate_optimized(test_loader, error)

        progress_thread.join()

        # Update fault rounds statistics
        for stats in self.performance:
            stats.update()

    def _pre_run(self) -> None:
        if not self.rounds:
            self.rounds = [FaultRound()]

        # Reset fault round variables
        self.r_idx = 0
        self.orounds.clear()
        self.rgroups.clear()
        self.handles.clear()
        self.performance.clear()

        # Create faulty version of network
        self.faulty = deepcopy(self.golden)
        self.faulty.forward = MethodType(Campaign._forward_opt_wrapper(self.layers_info, self.slayer), self.faulty)

        for r, round in enumerate(self.rounds):
            # Create optimized fault rounds from rounds
            oround = round.optimized(self.layers_info)
            self.orounds.append(oround)

            # Group fault rounds per earliest faulty layer
            self.rgroups.setdefault(oround.early_name, list())
            self.rgroups[oround.early_name].append(r)

            # Create faulty network instances for fault rounds
            self._perturb_net(oround)

            # Create statistics for fault rounds
            self.performance.append(spikeStats())

        # Sort fault round goups in ascending order of group earliest layer
        self.rgroups = dict(sorted(self.rgroups.items(), key=lambda item: -1 if item[0] is None else self.layers_info.index(item[0])))

    def _perturb_net(self, round: FaultRound) -> None:
        ind_neu = FaultTarget.Z.get_index()  # 0
        ind_par = FaultTarget.P.get_index()  # 2
        ind_syn = FaultTarget.W.get_index()  # 1

        for layer_name in self.layers_info.get_injectables():
            self.handles.setdefault(layer_name, [[None] * 2 for _ in range(3)])
            layer = getattr(self.faulty, layer_name)

            # Neuronal faults
            if round.any_neuronal(layer_name):
                # Neuronal faults for last layer are evaluated directly on faulty network's output
                if not self.layers_info.is_output(layer_name) and not self.handles[layer_name][ind_neu][0]:
                    following_layer = getattr(self.faulty, self.layers_info.get_following(layer_name))

                    # Register neuron fault pre-hooks
                    pre_hook = self._neuron_pre_hook_wrapper(layer_name)
                    self.handles[layer_name][ind_neu][0] = following_layer.register_forward_pre_hook(pre_hook)

                # Parametric faults (subset of neuronal faults)
                if round.any_parametric(layer_name):
                    for fault in round.search_parametric(layer_name):
                        # Create parametric faults' dummy layers
                        fault.model.param_perturb(self.slayer)

                    if not self.handles[layer_name][ind_par][1]:
                        # Register parametric fault hooks
                        hook = self._parametric_hook_wrapper(layer_name)
                        self.handles[layer_name][ind_par][1] = layer.register_forward_hook(hook)

            # Synaptic faults
            if round.any_synaptic(layer_name) and not any(self.handles[layer_name][ind_syn]):
                # Register synapse fault pre-hooks
                pre_hook = self._synaptic_pre_hook_wrapper(layer_name)
                self.handles[layer_name][ind_syn][0] = layer.register_forward_pre_hook(pre_hook)

                # Register synapse fault hooks
                hook = self._synaptic_hook_wrapper(layer_name)
                self.handles[layer_name][ind_syn][1] = layer.register_forward_hook(hook)

    def _evaluate_single(self, test_loader: DataLoader, error: snn.loss = None) -> None:
        is_out_faulty = self.orounds[self.r_idx].is_out_faulty
        out_neuron_callable = self._neuron_pre_hook_wrapper(self.layers_info.order[-1])

        for b, (input, target, label) in enumerate(test_loader):
            # FIXME: Hooks attached on dropout layer are called multiple times if the layer is reused
            output = self.faulty(input.to(self.device))
            if is_out_faulty:
                out_neuron_callable(None, (output,))

            self._advance_performance(self.performance[self.r_idx], output, target, label, error)

            with self.progress_lock:
                self.progress.step()
                self.progress.set_batch(b)

    def _evaluate_optimized(self, test_loader: DataLoader, error: snn.loss = None) -> None:
        out_neuron_callable = self._neuron_pre_hook_wrapper(self.layers_info.order[-1])

        for b, (input, target, label) in enumerate(test_loader):  # For each batch
            # Store golden spikes
            golden_spikes = [input.to(self.device)]
            for layer_idx in range(len(self.layers_info)):
                golden_spikes.append(self.golden(golden_spikes[layer_idx], layer_idx, layer_idx))

            for round_group in self.rgroups.values():  # For each fault round group
                for self.r_idx in round_group:  # For each fault round
                    oround = self.orounds[self.r_idx]

                    if oround:
                        late_out = self.faulty(golden_spikes[oround.early_idx], oround.early_idx, oround.late_idx)
                        if oround.is_out_faulty:
                            out_neuron_callable(None, (late_out,))
                            output = late_out
                        else:
                            # Early stop optimization
                            late_next_out = self.faulty(late_out, oround.late_idx + 1, oround.late_idx + 1)
                            early_stop = torch.equal(late_next_out, golden_spikes[oround.late_idx + 2])
                            output = golden_spikes[-1] if early_stop else self.faulty(late_next_out, oround.late_idx + 2)
                    else:
                        output = golden_spikes[-1]

                    self._advance_performance(self.performance[self.r_idx], output, target, label, error)

                    with self.progress_lock:
                        self.progress.step()

            with self.progress_lock:
                self.progress.set_batch(b)

    def _advance_performance(self, stats: spikeStats, output: Tensor, target: Tensor, label: Tensor, error: snn.loss = None) -> None:
        stats.testing.correctSamples += torch.sum(snn.predict.getClass(output) == label).item()
        stats.testing.numSamples += len(label)
        if error:
            stats.testing.lossSum += error.numSpikes(output, target.to(self.device)).cpu().item()

    def run_complete(self, test_loader: DataLoader, fault_model: FaultModel, layer_names: Iterable[str] = None, error: snn.loss = None) -> None:
        if layer_names is not None and not isinstance(layer_names, Iterable) or isinstance(layer_names, str):
            raise TypeError(f"'{type(layer_names).__name__}' object for layer_names arguement is not iterable or is str")

        if layer_names:
            lay_names_inj = [lay_name for lay_name in layer_names
                             if self.layers_info.is_injectable(lay_name)]
        else:
            lay_names_inj = self.layers_info.get_injectables()

        is_syn = fault_model.is_synaptic()
        self.rounds.clear()

        for lay_name in lay_names_inj:
            lay_shape = self.layers_info.get_shapes(is_syn, lay_name)
            for k in range(lay_shape[0] if is_syn else 1):
                for l in range(lay_shape[0 + is_syn]):          # noqa: E741
                    for m in range(lay_shape[1 + is_syn]):
                        for n in range(lay_shape[2 + is_syn]):
                            self.then_inject(
                                [Fault(fault_model, FaultSite(lay_name, (k if is_syn else slice(None), l, m, n)))])

        self.run(test_loader, error)

    @staticmethod
    def _forward_opt_wrapper(layers_info: LayersInfo, slayer: spikeLayer) -> Callable[[Tensor, Optional[int], Optional[int]], Tensor]:
        def forward_opt(self: nn.Module, spikes_in: Tensor, start_layer_idx: int = None, end_layer_idx: int = None) -> Tensor:
            start_idx = 0 if start_layer_idx is None else start_layer_idx
            end_idx = (len(layers_info) - 1) if end_layer_idx is None else end_layer_idx
            subject_layers = [lay_name for lay_idx, lay_name in enumerate(layers_info.order)
                              if start_idx <= lay_idx <= end_idx]

            spikes = spikes_in
            for layer_name in subject_layers:
                layer = getattr(self, layer_name)
                spikes = layer(spikes)

                if layers_info.types[layer_name] is not snn.slayer._dropoutLayer:
                    # Dropout layers are not useful in inference but they may have registered pre-hooks
                    spikes = slayer.spike(slayer.psp(spikes))

            return spikes

        return forward_opt

    def _neuron_pre_hook_wrapper(self, layer_name: str) -> Callable[[nn.Module, tuple[Tensor, ...]], None]:
        def neuron_pre_hook(_, args: tuple[Tensor, ...]) -> None:
            prev_spikes_out = args[0]

            for fault in self.orounds[self.r_idx].search_neuronal(layer_name):
                for site in fault.sites:
                    ind = site.unroll()
                    prev_spikes_out[ind] = fault.model.perturb(prev_spikes_out[ind], site)

        return neuron_pre_hook

    def _parametric_hook_wrapper(self, layer_name: str) -> Callable[[nn.Module, tuple[Tensor, ...], Tensor], None]:
        def parametric_hook(_, __, spikes_out: Tensor) -> None:
            for fault in self.orounds[self.r_idx].search_parametric(layer_name):
                flayer = fault.model.flayer
                fspike_out = flayer.spike(flayer.psp(spikes_out))

                for site in fault.sites:
                    fault.model.args[0][site] = fspike_out[site.unroll()]

        return parametric_hook

    # Perturb weights for layer's synapse faults
    def _synaptic_pre_hook_wrapper(self, layer_name: str) -> Callable[[nn.Module, tuple[Tensor, ...]], None]:
        def synaptic_pre_hook(layer: nn.Module, _) -> None:
            for fault in self.orounds[self.r_idx].search_synaptic(layer_name):
                for site in fault.sites:
                    ind = site.unroll()
                    with torch.no_grad():
                        layer.weight[ind] = fault.model.perturb_store(layer.weight[ind].item(), site)

        return synaptic_pre_hook

    # Restore weights after layer's synapse faults evaluation
    def _synaptic_hook_wrapper(self, layer_name: str) -> Callable[[nn.Module, tuple[Tensor, ...], Tensor], None]:
        def synaptic_hook(layer: nn.Module, _, __) -> None:
            for fault in self.orounds[self.r_idx].search_synaptic(layer_name):
                for site in fault.sites:
                    with torch.no_grad():
                        layer.weight[site.unroll()] = fault.model.restore(site)

        return synaptic_hook
