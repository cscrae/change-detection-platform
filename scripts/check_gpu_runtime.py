"""Verify real CUDA/cuDNN execution without loading a business model or imagery.

Run with backend-py/.venv-local/Scripts/python.exe. The temporary Conv graph
executes in the isolated model interpreter and is removed after the check.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

import onnx
from onnx import TensorProto, helper


PROJECT = Path(__file__).resolve().parents[1]
PROBE = r'''
import json
import os
import sys
import numpy as np
import onnxruntime as ort

ort.preload_dlls(directory=os.environ.get('CHANGE_CUDA_DLL_DIR', ''))
options = ort.SessionOptions()
options.add_session_config_entry('session.disable_cpu_ep_fallback', '1')
session = ort.InferenceSession(
    sys.argv[1], sess_options=options, providers=['CUDAExecutionProvider']
)
if 'CUDAExecutionProvider' not in session.get_providers():
    raise RuntimeError('CUDAExecutionProvider did not start')
result = session.run(None, {'x': np.ones((1, 1, 8, 8), dtype=np.float32)})[0]
np.testing.assert_allclose(result, np.full((1, 1, 6, 6), 9, dtype=np.float32))
print(json.dumps({
    'status': 'ok',
    'onnxruntime': ort.__version__,
    'providers': session.get_providers(),
    'operation': 'Conv',
    'output_shape': list(result.shape),
    'cpu_fallback': False,
}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--python',
        type=Path,
        default=PROJECT / 'model-runtime/.venv-gpu/Scripts/python.exe',
        help='Isolated ONNX Runtime GPU interpreter',
    )
    args = parser.parse_args()
    interpreter = args.python.resolve()
    if not interpreter.is_file():
        parser.error(f'GPU interpreter does not exist: {interpreter}')
    environment = dict(os.environ)
    if not environment.get('CHANGE_CUDA_DLL_DIR'):
        directories = environment.get('CHANGE_CUDA_DLL_DIRS', '')
        config = PROJECT / '.env.local.json'
        if not directories and config.is_file():
            directories = json.loads(config.read_text(encoding='utf-8-sig')).get(
                'CHANGE_CUDA_DLL_DIRS', ''
            )
        required = (
            'cublasLt64_12.dll', 'cublas64_12.dll', 'cufft64_11.dll',
            'cudart64_12.dll', 'cudnn64_9.dll',
        )
        for value in directories.split(';'):
            if not value.strip():
                continue
            directory = Path(value.strip())
            if not directory.is_absolute():
                directory = PROJECT / directory
            if all((directory / name).is_file() for name in required):
                environment['CHANGE_CUDA_DLL_DIR'] = str(directory.resolve())
                break
    if environment.get('CHANGE_CUDA_DLL_DIR'):
        environment['PATH'] = (
            environment['CHANGE_CUDA_DLL_DIR'] + os.pathsep + environment.get('PATH', '')
        )
    graph = helper.make_graph(
        [helper.make_node('Conv', ['x', 'w'], ['y'], kernel_shape=[3, 3])],
        'cuda-runtime-check',
        [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 1, 8, 8])],
        [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 1, 6, 6])],
        [helper.make_tensor('w', TensorProto.FLOAT, [1, 1, 3, 3], [1.0] * 9)],
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid('', 17)], ir_version=10
    )
    onnx.checker.check_model(model)
    with tempfile.TemporaryDirectory(prefix='cdp-cuda-') as directory:
        path = Path(directory) / 'runtime-check.onnx'
        onnx.save(model, path)
        completed = subprocess.run(
            [str(interpreter), '-c', PROBE, str(path)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=environment,
            timeout=60,
        )
    if completed.returncode:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
    # preload_dlls may emit diagnostics before the JSON result.
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
