import os
import sys
import argparse
from copy import deepcopy
from pathlib import Path

import torch
import onnx

FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
os.chdir(ROOT)

from core.yolo.util.torch_utils import select_device
from core.yolo.nn.tasks import attempt_load_weights
from core.yolo.nn.modules import Detect
from core.yolo.nn.modules.block import C2f


def convert(opt):
    """
    Converts a PyTorch model to ONNX format.

    Args:
        opt (argparse.Namespace): Parsed arguments with the following attributes:
            - weight (str): Path to the PyTorch model (.pt or .pth).
            - device (str): Device to use ('cpu' or 'cuda').
            - gpu_num (int): GPU number to use.
            - imgsz (list[int]): Image size for model input (e.g., [640, 640]).
            - simplify (bool): Flag to simplify the ONNX model.
            - opset (int): ONNX opset version.
            - fp16 (bool): Use FP16 precision if enabled.

    Raises:
        AssertionError: If `--fp16` is used without a GPU device.
        RuntimeError: If the ONNX model cannot be simplified.

    Returns:
        None: Saves the ONNX model to disk.
    """
    file = Path(opt.weight)

    # Device and Half Check
    device = select_device(device=opt.device, gpu_num=opt.gpu_num)
    fp16 = opt.fp16
    if fp16:
        assert device.type != 'cpu', '--fp16 only compatible with GPU export, i.e. use --device cuda'
    model = attempt_load_weights(opt.weight, device=device, inplace=True, fuse=True)

    # Input shape Check
    imgsz = opt.imgsz * 2 if len(opt.imgsz) == 1 else opt.imgsz
    im = torch.zeros(1, 3, *imgsz).to(device)

    # Update model
    model = deepcopy(model).to(device)
    for p in model.parameters():
        p.requires_grad = False
    model.eval()
    model.float()
    model = model.fuse()
    for m in model.modules():
        if isinstance(m, Detect):  # includes all Detect subclasses like Segment, Pose, OBB
            m.dynamic = False
            m.export = True
            m.format = 'onnx'
        elif isinstance(m, C2f):
            # EdgeTPU does not support FlexSplitV while split provides cleaner ONNX graph
            m.forward = m.forward_split

    y = None
    for _ in range(2):
        y = model(im)  # dry runs

    data_format = 'fp32'
    if fp16:
        print("It will apply fp16 precision.")
        im, model = im.half(), model.half()
        data_format = 'fp16'
    shape = tuple((y[0] if isinstance(y, tuple) else y).shape)
    print(f"Converting from {file} with output shape {shape}")

    f = str(file.with_suffix('.onnx'))
    f = os.path.splitext(f)[0] + f"-{data_format}" + os.path.splitext(f)[1]
    torch.onnx.export(
        model=model,
        args=im,
        f=f,
        verbose=False,
        opset_version=opt.opset,
        do_constant_folding=True,
        input_names=['images'],
        output_names=['output0'],
        dynamic_axes=None
    )

    # check
    model_onnx = onnx.load(f)   # load onnx model
    onnx.checker.check_model(model_onnx)    # check onnx model

    # Metadata
    d = {'stride': int(max(model.stride)), 'names': model.names}
    for k, v in d.items():
        meta = model_onnx.metadata_props.add()
        meta.key, meta.value = k, str(v)
    onnx.save(model_onnx, f)
    print(f"Converting and Saving success: {f}")

    # Simplify
    if opt.simplify:
        try:
            import onnxsim

            model_onnx, check = onnxsim.simplify(model_onnx)
            assert check, "Simplified ONNX model could not be validated"
            onnx.save(model_onnx, f)
            print(f"Simplifying and Saving success: {f}")
        except Exception as e:
            print(f"simplifier failure: {e}")


def arg_parse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-w', '--weight', required=True, help=".pt or .pth file path")
    parser.add_argument('-d', '--device', default='cpu', help='cpu or cuda')
    parser.add_argument('--gpu_num', default=0, type=int, help='0, 1, 2,...')
    parser.add_argument("--imgsz", type=int, nargs="+", default=[640, 640], help="image (h, w)",
                        choices=[640, 1280, [640, 640], [1280, 1280]])
    parser.add_argument('--simplify', action='store_true', help='ONNX: simplify model')
    parser.add_argument('--opset', type=int, default=17, help='ONNX: opset version')
    parser.add_argument('--fp16', action='store_true')
    return parser.parse_args()


if __name__ == "__main__":
    args = arg_parse()
    convert(args)
