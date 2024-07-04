import os
import torch

import slayerSNN as snn

import spikefi as sfi
import demo as cs


L = ['SF1']     # 'SF2', 'SF1', 'SC3', 'SC2', 'SC1', ''
P = ['theta', 'tauSr', 'tauRef']
C = range(10, 300, 10)  # Percentage of nominal value

fnetname = cs.get_fnetname(trial='4')
net: cs.Network = torch.load(os.path.join(cs.OUT_DIR, cs.CASE_STUDY, fnetname))
net.eval()

for lay_name in L:
    for param in P:
        for c in C:
            cmpn_name = fnetname.removesuffix('.pt') + f"_neuron_{param}_{lay_name or 'ALL'}_c{c}"
            cmpn = sfi.Campaign(net, cs.shape_in, net.slayer, name=cmpn_name)

            cmpn.inject_complete(sfi.fm.ParametricNeuron(param, c / 100.0), [lay_name])

            print(cmpn.name)
            cmpn.run(cs.test_loader, error=snn.loss(cs.net_params).to(cmpn.device))
            print(f"{cmpn.duration : .2f} secs")

            cmpn.save()
