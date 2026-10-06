# Eye Calibration

`eye_calibration.json` is a generated installation artifact. It is intentionally
absent from the repository until enrollment is performed on the final camera,
resolution, aspect ratio, orientation and mounting position.

Run from the project root, using an environment that contains MediaPipe:

```bash
python3 calibrate_eyes.py --output config/eye_calibration.json
```

Enrollment has explicit open and closed phases. Each eye needs at least 30 valid
samples per phase. Open references use P80, closed references use P20, and both
eyes must have an open/closed separation of at least `0.03` EAR.

Do not copy this file between camera geometries or mounts. The runtime remains
`UNKNOWN` when the artifact is absent or invalid.
