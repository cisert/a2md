from a2mdio.molecules import Mol2
from a2mdnet.data import convert_label2tensor
from a2md.utils import integrate_from_dict
from a2mdnet.data import match_fun_names
from a2mdio.utils import eval_volume
from a2md import LEBEDEV_DESIGN
import warnings
import torch
from typing import Callable, Dict
import math

def get_charges(l, t, i_iso, i_aniso, iso, aniso, device):
    """

    :param l:
    :param t:
    :param i_iso:
    :param i_aniso:
    :param iso:
    :param aniso:
    :param device:
    :return:
    """

    # Defining problems
    n_batch = l.size(0)
    n_atoms = l.size(1)
    n_bond = t.size(1)
    n_bond_funs = int(i_aniso.size(2) / 2)

    # Calculating function charges
    charge_iso = (i_iso * iso)
    charge_aniso = (i_aniso * aniso)
    q_iso = charge_iso.sum(2)
    # Calculating charges per atom
    # To do so, the topology tensor is expanded to match the shape of the anisotric charge tensor
    # Then a range tensor is added to allow to map index in flat vectors
    # Finally, two operations are employed to map back the anisotropic charge to each atom
    #       1. scatter add. Sums all the positions related to the same index and stores them in that given index
    #       2. masked_scatter. Gets all the non zero values and stores them in the output

    t_r = t.reshape(n_batch, n_bond_funs * n_bond, 1).expand(n_batch, n_bond_funs * n_bond, n_bond_funs).reshape(
        n_batch, n_bond, 2 * n_bond_funs)
    r = (torch.arange(0, n_batch, device=device) * n_atoms).unsqueeze(1).unsqueeze(2).expand(n_batch, n_bond,
                                                                                             2 * n_bond_funs)
    t_r.masked_scatter_(t_r != -1, t_r.masked_select(t_r != -1) + r.masked_select(t_r != -1))
    t_r[t_r == -1] = 0
    q_buffer = torch.zeros_like(charge_aniso).flatten()
    t_rf = t_r.flatten()
    ca_f = charge_aniso.flatten()
    q_buffer = q_buffer.scatter_add(0, t_rf, ca_f)
    q_aniso = torch.zeros_like(q_iso)
    q_aniso = q_aniso.masked_scatter(l != - 1, q_buffer.masked_select(q_buffer != 0.0))

    q_per_atom = q_aniso + q_iso
    return q_per_atom


class Parametrizer:

    def __init__(self, model, device):
        self.model = model
        self.device = device

    def parametrize(self, mol, params):
        """

        :param mol:
        :param params
        :return:
        """
        if not isinstance(mol, Mol2):
            try:
                mol = Mol2(file=mol)
            except ValueError:
                raise IOError("unknown format for mol. Please, use Mol2 or str")
        coords = torch.tensor(mol.get_coordinates(), device=self.device, dtype=torch.float).unsqueeze(0)
        labels = convert_label2tensor(mol.get_atomic_numbers(), device=self.device).unsqueeze(0)
        connectivity = torch.tensor(mol.get_bonds(), dtype=torch.long, device=self.device).unsqueeze(0)
        charge = torch.tensor(mol.charges + mol.atomic_numbers, dtype=torch.float, device=self.device).unsqueeze(0)
        natoms = mol.get_number_atoms()
        nbonds = mol.get_number_bonds()
        connectivity -= 1

        int_iso = torch.zeros(1, natoms, 2)
        int_aniso = torch.zeros(1, nbonds, 4)

        for fun in params:
            center = fun['center']
            funtype, pos = match_fun_names(fun)
            if funtype == 'core':
                charge[0, center] -= integrate_from_dict(fun)
            elif funtype in 'iso':
                int_iso[0, center, pos] = integrate_from_dict(fun)
            elif funtype in 'aniso':
                idx, col = self.match_bond(connectivity, fun)
                int_aniso[0, idx, col + pos] = integrate_from_dict(fun)

        int_iso = int_iso.to(self.device)
        int_aniso = int_aniso.to(self.device)

        _, _, iso_out, aniso_out = self.model.forward_coefficients(labels, connectivity, coords, charge, int_iso,
                                                                   int_aniso)

        if iso_out.is_cuda:
            iso_out = iso_out.squeeze(0).cpu().data.numpy()
            aniso_out = aniso_out.squeeze(0).cpu().data.numpy()
        else:
            iso_out = iso_out.squeeze(0).data.numpy()
            aniso_out = aniso_out.squeeze(0).data.numpy()

        for fun in params:
            center = fun['center']
            funtype, pos = match_fun_names(fun)
            if funtype == 'core':
                continue
            if funtype == 'iso':
                fun['coefficient'] = iso_out[center, pos].item()
            elif funtype == 'aniso':
                idx, col = self.match_bond(connectivity, fun)
                fun['coefficient'] = aniso_out[idx, col + pos].item()

        return params

    @staticmethod
    def match_bond(topology, fun):
        topology = topology.squeeze(0)
        for j in range(topology.shape[0]):
            c1 = (topology[j, 0] == fun['center']) and (
                    topology[j, 1] == fun['bond']
            )
            c2 = (topology[j, 1] == fun['center']) and (
                    topology[j, 0] == fun['bond']
            )
            if c1:
                return j, 0
            if c2:
                return j, 0 + 2

        warnings.warn("bond was not matched")


class CoordinatesSampler:
    def __init__(
            self, device: torch.device, dtype: torch.dtype,
            sampler: str, sampler_args: Dict
    ):
        """
        coordinates sampler
        ---
        performs a 3d coordinates sample given some initical coordinates
        """

        self.internal_methods = dict(
            random=CoordinatesSampler.random_box,
            spheres=CoordinatesSampler.spheres
        )
        self.sampler = self.internal_methods[sampler]
        self.sampler_args = sampler_args
        self.device = device
        self.dtype = dtype

    @staticmethod
    def principal_components(coords: torch.Tensor):
        mean = coords.mean(1, keepdim=True)
        x = coords - mean
        n = coords.size()[1]
        c = x.transpose(1, 2) @ x / n
        eig, eiv = torch.symeig(c, eigenvectors=True)
        eivp = eiv.inverse()
        x = (eivp @ x.transpose(1, 2)).transpose(1, 2)
        # x += mean
        return x, eiv, mean

    @staticmethod
    def random_box(coords, device, dtype, n_sample=1000, spacing=6.0):
        r = torch.rand(coords.size()[0], n_sample, 3, device=device, dtype=dtype)
        rotcoords, eivp, mean = CoordinatesSampler.principal_components(coords)
        box_min = rotcoords.min(1, keepdim=True)[0] - spacing
        box_max = rotcoords.max(1, keepdim=True)[0] + spacing
        diff = box_max + (box_min * -1)
        r = (r * diff) + box_min
        r = (eivp @ r.transpose(1, 2)).transpose(1, 2)
        r += mean
        return r

    @staticmethod
    def spheres(coords, device, dtype, grid='coarse', resolution=10, max_radius=10.0):
        import numpy as np
        n = coords.size()[0]
        m = coords.size()[1]

        design = np.loadtxt(LEBEDEV_DESIGN[grid])
        design = torch.tensor(design, device=device, dtype=dtype)
        phi, psi, _ = design.split(1, dim=1)
        phi = (phi / 180.0) * math.pi
        psi = (psi / 180.0) * math.pi
        design_size = phi.size(0)
        sphere_x = (phi.cos() * psi.sin()).flatten()
        sphere_y = (phi.sin() * psi.sin()).flatten()
        sphere_z = (psi.cos()).flatten()

        sphere = torch.stack([sphere_x, sphere_y, sphere_z], dim=1)
        radius = torch.arange(resolution, dtype=torch.float, device=device) / resolution
        radius = 0.1 + (radius * (max_radius - 0.1))
        n_spheres = radius.size()[0]
        radius = radius.unsqueeze(1).unsqueeze(2)

        sphere = sphere.unsqueeze(0).expand(n_spheres, design_size, 3)
        concentric_spheres = radius * sphere
        concentric_spheres = concentric_spheres.reshape(-1, 3)
        concentric_spheres = concentric_spheres.unsqueeze(0)

        ms = concentric_spheres.size()[1]

        coords = coords.reshape(-1, 3)
        coords_x, coords_y, coords_z = torch.split(coords, 1, dim=1)
        mask_x = coords_x == 0
        mask_y = coords_y == 0
        mask_z = coords_z == 0
        mask = mask_x * mask_y * mask_z
        n_zeros = mask.sum()
        mean_coords = coords.mean(dim=0)
        std_coords = coords.std(dim=0)
        off_centers = mean_coords + (torch.randn((n_zeros, 3), dtype=dtype, device=device) * std_coords)

        coords = torch.masked_scatter(coords, mask, off_centers)
        coords = coords.unsqueeze(1)
        coords = coords.expand(n * m, ms, 3)

        concentric_spheres = concentric_spheres + coords
        concentric_spheres = concentric_spheres.reshape(n, m * ms, 3)
        return concentric_spheres

    def __call__(self, coords):
        r = self.sampler(
            coords=coords, device=self.device, dtype=self.dtype, **self.sampler_args
        )
        return r


def torch_eval_volume(fun: Callable, resolution:float, steps:int, device: torch.device):
    modfun = lambda x: fun(
        torch.tensor(x, device=device, dtype=torch.float).unsqueeze(0)).data.cpu().numpy()
    dx = eval_volume(modfun, resolution, steps, shift=[0, 0, 0])
    return dx
