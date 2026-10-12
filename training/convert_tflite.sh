#!/usr/bin/env bash
# Convert a trained ONNX model to TFLite for the phone app.
# Usage: ./convert_tflite.sh tomato
# Needs: pip install onnx2tf tensorflow onnx onnxsim onnx_graphsurgeon sng4onnx
set -euo pipefail
CROP="$1"
SRC="out/${CROP}/model.onnx"
DST="../mobile/assets/models"
mkdir -p "$DST" "out/${CROP}/tflite"
# onnx2tf turns the NCHW PyTorch model into an NHWC TFLite model ([1,224,224,3] input)
onnx2tf -i "$SRC" -o "out/${CROP}/tflite" -b 1
cp "out/${CROP}/tflite/model_float16.tflite" "$DST/${CROP}.tflite"
cp "out/${CROP}/labels.txt" "$DST/${CROP}_labels.txt"
echo "Copied ${CROP}.tflite and ${CROP}_labels.txt to $DST"
