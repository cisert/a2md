from a2mdnet import modules
from a2mdnet.functions import APEV
import torch
import torch.nn as nn
import warnings
warnings.filterwarnings('ignore')
DEVICE = torch.device('cpu')
DEFAULT_ARCHITECTURE = dict(
    common_net=[96, 36, 6],
    atom_net=[6, 2],
    bond_net=[22, 2],
    subnet=0
)
PAIR_FEATURES = dict(
    EtaR = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
    ShfR = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5],
    Rc = 6.0
)
LR = 1e-5
EPOCHS = 100
BETAS = (0.9, 0.999)
WEIGHT_DECAY = 1e-2


class PairsNormA2MDDensity(nn.Module):

    def __init__(self, architecture):

        super(PairsNormA2MDDensity, self).__init__()

        # self.normalize = normalize

        try:

            common_net_architecture = architecture['common_net']
            atom_net_architecture = architecture['atom_net']
            bond_net_architecture = architecture['bond_net']
            subnet = architecture['subnet']

        except KeyError:
            raise IOError("architecture dict did not contain the right fields")

        self.common_net = modules.TorchElementSpecificA2MDNN(
            nodes=common_net_architecture, elements=[1, 6, 7, 8], device=DEVICE
        )
        self.atom_net = modules.TorchElementSpecificA2MDNN(
            nodes=atom_net_architecture, elements=[1, 6, 7, 8], device=DEVICE
        )
        self.bond_net = modules.TorchPairDistances(
            nodes=bond_net_architecture, elements=[1, 6, 7, 8], device=DEVICE
        )
        self.feature_extractor = modules.TorchaniFeats(net=subnet, device=DEVICE)

        for param in self.feature_extractor.parameters():
            param.requires_grad = False

        self.normalizer = modules.QPChargeNormalization(device=device)
        self.pair_distances = APEV(
            output_size=10, parameters=PAIR_FEATURES, device=device
        )

    @staticmethod
    def reverse_connectivity(conn):
        """

        :param conn:
        :return:
        """
        return torch.flip(conn, dims=(2, ))

    def forward_common_net(self, *x):
        """

        :param x:
        :return:
        """
        labels, coordinates = x

        labels, ta_feats = self.feature_extractor(labels, coordinates)
        labels, common_layer = self.common_net(labels, ta_feats)

        return labels, common_layer

    def forward_atom_net(self, *x):
        """

        :param x:
        :return:
        """
        labels, common_layer = x

        labels, atom_targets = self.atom_net(labels, common_layer)

        return labels, atom_targets

    def forward_pairs_distance(self, *x):
        """

        :param x:
        :return:
        """
        con, coords = x

        con, pairs = self.pair_distances(con, coords)

        return con, pairs

    def forward_bond_net(self, *x):
        """

        :param x:
        :return:
        """
        labels, connectivity, common_layer, pairs = x

        reverse_con = self.reverse_connectivity(connectivity)

        labels, connectivity, bond_targets_forward = self.bond_net(labels, connectivity, common_layer, pairs)
        labels, connectivity, bond_targets_reverse = self.bond_net(
            labels, reverse_con, common_layer, pairs
        )

        bond_targets = torch.cat([bond_targets_forward, bond_targets_reverse], dim=2)

        return labels, connectivity, bond_targets

    def forward(self, *x):
        """

        :param x:
        :return:
        """

        labels, connectivity, coordinates, charges, int_iso, int_aniso = x
        connectivity, pairs = self.forward_pairs_distance(connectivity, coordinates)
        labels, common_layer = self.forward_common_net(labels, coordinates)
        labels, atom_targets = self.forward_atom_net(labels, common_layer)
        labels, connectivity, bond_targets = self.forward_bond_net(labels, connectivity, common_layer, pairs)
        atom_targets, bond_targets = self.normalizer.forward(
            atom_targets,
            bond_targets,
            int_iso,
            int_aniso,
            charges
        )

        return labels, connectivity, atom_targets, bond_targets

    def get_atom_feats(self, *x):
        """

        :param x:
        :return:
        """
        labels, coordinates = x
        labels, common_layer = self.forward_common_net(labels, coordinates)
        labels, atom_targets = self.forward_atom_net(labels, common_layer)
        return labels, atom_targets

    def get_bond_feats(self, *x):
        """

        :param x:
        :return:
        """
        labels, connectivity, coordinates = x
        labels, common_layer = self.forward_common_net(labels, coordinates)
        labels, connectivity, atom_targets = self.forward_bond_net(labels, connectivity, common_layer)
        return labels, connectivity, atom_targets


if __name__ == "__main__":

    from a2mdnet.data import CompleteSetDensityParams
    from torch.utils import data
    import torch

    device = torch.device('cpu')
    params = dict(
        batch_size=64,
        shuffle=False,
        num_workers=0
    )

    csdp_t = CompleteSetDensityParams(
        device=device, dtype=torch.float, kind='curated_training', number=1000, charges=True, integrals=True
    )

    csdp_t_dl = data.DataLoader(csdp_t, **params)
    model = PairsNormA2MDDensity(architecture=DEFAULT_ARCHITECTURE)

    # sgd_opt = torch.optim.SGD(lr=1e-5, params=model.parameters())
    lr = 1e-4
    gamma = 1e-2
    for t in range(10):
        for l_, c_, x_, q_, i_iso, i_aniso, iso, aniso in csdp_t_dl:

            _, _, iso_targets, aniso_targets = model.forward(l_, c_, x_, q_, i_iso, i_aniso)

            l2 = torch.pow(iso_targets - iso, 2.0).sum() + torch.pow(aniso_targets - aniso, 2.0).sum()

            l2.backward()

            with torch.no_grad():
                for p in model.parameters():
                    if p.grad is None:
                        pass
                    else:
                        p -= lr * p.grad

            print(l2.item())
            model.zero_grad()
