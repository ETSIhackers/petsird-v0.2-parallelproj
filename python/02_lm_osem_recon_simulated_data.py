#  Copyright (C) 2024 University College London
#
#  SPDX-License-Identifier: Apache-2.0

# basic plotting of the scanner geometry
# preliminary code!

import array_api_compat.numpy as xp
import numpy.typing as npt
import petsird
import parallelproj

from petsird_helpers import (
    get_module_and_element,
    get_detection_efficiency,
)

from pathlib import Path
import argparse


def transform_to_mat44(
    transform: petsird.RigidTransformation,
) -> npt.NDArray[xp.float32]:
    return xp.vstack([transform.matrix, [0, 0, 0, 1]])


def mat44_to_transform(mat: npt.NDArray[xp.float32]) -> petsird.RigidTransformation:
    return petsird.RigidTransformation(matrix=mat[0:3, :])


def coordinate_to_homogeneous(coord: petsird.Coordinate) -> npt.NDArray[xp.float32]:
    return xp.hstack([coord.c, 1])


def homogeneous_to_coordinate(
    hom_coord: npt.NDArray[xp.float32],
) -> petsird.Coordinate:
    return petsird.Coordinate(c=hom_coord[0:3])


def mult_transforms(
    transforms: list[petsird.RigidTransformation],
) -> petsird.RigidTransformation:
    """multiply rigid transformations"""
    mat = xp.array(
        ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)),
        dtype="float32",
    )

    for t in reversed(transforms):
        mat = xp.matmul(transform_to_mat44(t), mat)
    return mat44_to_transform(mat)


def mult_transforms_coord(
    transforms: list[petsird.RigidTransformation], coord: petsird.Coordinate
) -> petsird.Coordinate:
    """apply list of transformations to coordinate"""
    # TODO better to multiply with coordinates in sequence, as first multiplying the matrices
    hom = xp.matmul(
        transform_to_mat44(mult_transforms(transforms)),
        coordinate_to_homogeneous(coord),
    )
    return homogeneous_to_coordinate(hom)


def transform_BoxShape(
    transform: petsird.RigidTransformation, box_shape: petsird.BoxShape
) -> petsird.BoxShape:

    return petsird.BoxShape(
        corners=[mult_transforms_coord([transform], c) for c in box_shape.corners]
    )


def parse_int_tuple(arg):
    return tuple(map(int, arg.split(",")))


def parse_float_tuple(arg):
    return tuple(map(float, arg.split(",")))


# %%

parser = argparse.ArgumentParser()
parser.add_argument("--lm_fname", type=str, default="my_lm_sim/simulated_lm_file.bin")
parser.add_argument("--num_epochs", type=int, default=5)
parser.add_argument("--num_subsets", type=int, default=20)
parser.add_argument("--img_shape", type=parse_int_tuple, default=(100, 100, 11))
parser.add_argument("--voxel_size", type=parse_float_tuple, default=(1.0, 1.0, 1.0))
parser.add_argument("--fwhm_mm", type=float, default=1.5)
parser.add_argument("--output_dir", type=str, default="my_lm_sim")

args = parser.parse_args()

lm_fname = args.lm_fname
num_epochs = args.num_epochs
num_subsets = args.num_subsets
img_shape = args.img_shape
voxel_size = args.voxel_size
fwhm_mm = args.fwhm_mm
output_dir = Path(args.output_dir)

dev = "cpu"

if not output_dir.exists():
    output_dir.mkdir(parents=True)

# %%
if not Path(lm_fname).exists():
    raise FileNotFoundError(
        f"{args.lm_fname} not found. Create it first using the generator."
    )

# %%
# read the scanner geometry


reader = petsird.BinaryPETSIRDReader(lm_fname)
header = reader.read_header()

# %%
# check whether we only have 1 type of module
assert (
    len(header.scanner.scanner_geometry.replicated_modules) == 1
), "Only scanners with 1 module type supported yet"

# %%
# lists where we store the detecting element coordinates and transforms for each module
# the list has one entry per module

det_element_center_list = []

# %%
# read the LOR endpoint coordinates for each detecting element in each crystal
# we assume that the LOR endpoint corresponds to the center of the BoxShape

for rep_module in header.scanner.scanner_geometry.replicated_modules:
    det_el = rep_module.object.detecting_elements

    num_modules = len(rep_module.transforms)

    for i_mod, mod_transform in enumerate(rep_module.transforms):
        for rep_volume in det_el:

            det_element_centers = xp.zeros(
                (len(rep_volume.transforms), 3), dtype="float32"
            )

            num_el_per_module = len(rep_volume.transforms)

            for i_el, el_transform in enumerate(rep_volume.transforms):

                combined_transform = mult_transforms([mod_transform, el_transform])
                transformed_boxshape = transform_BoxShape(
                    combined_transform, rep_volume.object.shape
                )

                transformed_boxshape_vertices = xp.array(
                    [c.c for c in transformed_boxshape.corners]
                )

                det_element_centers[i_el, ...] = transformed_boxshape_vertices.mean(
                    axis=0
                )
            det_element_center_list.append(det_element_centers)

# create a list of the element detection efficiencies per module
# this is a simple re-ordering of the detection efficiencies array which
# makes the access easier
# we assume that all modules have the same number of detecting elements
det_el_efficiencies = [
    header.scanner.detection_efficiencies.det_el_efficiencies[
        i * num_el_per_module : (i + 1) * num_el_per_module, 0
    ]
    for i in range(num_modules)
]

num_tofbins = len(header.scanner.tof_bin_edges) - 1
tofbin_width = header.scanner.tof_bin_edges[1] - header.scanner.tof_bin_edges[0]
sigma_tof = header.scanner.tof_resolution / 2.35

tof_params = parallelproj.TOFParameters(
    num_tofbins=num_tofbins, tofbin_width=tofbin_width, sigma_tof=sigma_tof
)

assert num_tofbins % 2 == 1, "Number of TOF bins must be odd"
# %%
# calculate the sensitivity image
print("Calculating sensitivity image")

# we loop through the symmetric group ID look up table to see which module pairs
# are in coincidence


sig = fwhm_mm / (2.35 * xp.asarray(voxel_size, device=dev))
res_model = parallelproj.GaussianFilterOperator(img_shape, sigma=sig)

sens_img = xp.zeros(img_shape, dtype="float32")

for i in range(num_modules):
    for j in range(num_modules):
        sgid = header.scanner.detection_efficiencies.module_pair_sgidlut[i, j]

        if sgid >= 0:
            print(f"mod1 {i:03}, mod2 {j:03}, SGID {sgid:03}", end="\r")

            start_det_el = det_element_center_list[i]
            end_det_el = det_element_center_list[j]

            # create an array of that contains all possible combinations of start and end detecting element coordinates
            # these define all possible LORs between the two modules
            start_coords = xp.repeat(start_det_el, len(end_det_el), axis=0)
            end_coords = xp.tile(end_det_el, (len(start_det_el), 1))

            proj = parallelproj.ListmodePETProjector(
                start_coords, end_coords, img_shape, voxel_size
            )
            proj.tof_parameters = tof_params

            # get the module pair efficiencies - asumming that we only use 1 energy bin
            module_pair_eff = (
                header.scanner.detection_efficiencies.module_pair_efficiencies_vector[
                    sgid
                ].values[:, 0, :, 0]
            ).ravel()

            start_el_eff = xp.repeat(det_el_efficiencies[i], len(end_det_el), axis=0)
            end_el_eff = xp.tile(det_el_efficiencies[j], (len(start_det_el)))

            for tofbin in xp.arange(-(num_tofbins // 2), num_tofbins // 2 + 1):
                # print(tofbin)
                proj.event_tofbins = xp.full(
                    start_coords.shape[0], tofbin, dtype="int32"
                )
                proj.tof = True
                sens_img += proj.adjoint(start_el_eff * end_el_eff * module_pair_eff)

print("")

# for some reason we have to divide the sens image by the number of TOF bins
# right now unclear why that is
sens_img = res_model.adjoint(sens_img)

# %%
# read all coincidence events
print("Reading LM events")

num_prompts = 0
event_counter = 0
num_tof_bins = header.scanner.number_of_tof_bins()

xstart = []
xend = []
tof_bin = []
effs = []

for i_time_block, time_block in enumerate(reader.read_time_blocks()):
    if isinstance(time_block, petsird.TimeBlock.EventTimeBlock):
        num_prompts += len(time_block.value.prompt_events)

        for i_event, event in enumerate(time_block.value.prompt_events):
            event_mods_and_els = get_module_and_element(
                header.scanner.scanner_geometry, event.detector_ids
            )

            event_start_coord = det_element_center_list[event_mods_and_els[0].module][
                event_mods_and_els[0].el
            ]
            xstart.append(event_start_coord)

            event_end_coord = det_element_center_list[event_mods_and_els[1].module][
                event_mods_and_els[1].el
            ]
            xend.append(event_end_coord)

            # get the event efficiencies
            effs.append(get_detection_efficiency(header.scanner, event))
            # get the signed event TOF bin (0 is the central bin)
            tof_bin.append(event.tof_idx - num_tof_bins // 2)

            event_counter += 1

reader.close()

xstart = xp.asarray(xstart, device=dev)
xend = xp.asarray(xend, device=dev)
effs = xp.asarray(effs, device=dev)
tof_bin = xp.asarray(tof_bin, device=dev)


# %%
# run a LM OSEM recon
recon = xp.ones(img_shape, dtype="float32")

lm_subset_projs = []
subset_slices = [slice(i, None, num_subsets) for i in range(num_subsets)]

for i_subset, sl in enumerate(subset_slices):
    lm_subset_projs.append(
        parallelproj.ListmodePETProjector(
            xstart[sl, :], xend[sl, :], img_shape, voxel_size
        )
    )
    lm_subset_projs[i_subset].tof_parameters = tof_params
    lm_subset_projs[i_subset].event_tofbins = tof_bin[sl]
    lm_subset_projs[i_subset].tof = True

for i_epoch in range(num_epochs):
    for i_subset, sl in enumerate(subset_slices):
        print(
            f"it {(i_epoch +1):03} / {num_epochs:03}, ss {(i_subset+1):03} / {num_subsets:03}",
            end="\r",
        )
        lm_exp = effs[sl] * lm_subset_projs[i_subset](res_model(recon))
        tmp = num_subsets * res_model(
            lm_subset_projs[i_subset].adjoint(effs[sl] / lm_exp)
        )
        recon *= tmp / sens_img

print("")

opath = output_dir / f"lm_osem_{num_epochs}_{num_subsets}.npy"
xp.save(opath, recon)
print(f"LM OSEM recon saved to {opath}")
