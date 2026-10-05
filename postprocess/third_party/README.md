# third_party
- `rt_model_inference/` — rt-model-inference 1.0.1 (PyPI, MIT, Björn Brandenburg, https://lime.mpi-sws.org), the reference
  implementation of LiME / LiME-IT's arrival-model inference (periodic PF/CF, arrival curves). Unpacked from the wheel
  unchanged on 2026-10-04 (the wheel declares Python >= 3.12; the code runs on 3.10). Used by postprocess/limeit_models.py so
  that the LiME-IT comparison uses the authors' algorithms. Cite RTAS'25 (LiME) and RTAS'26 (LiME-IT).
