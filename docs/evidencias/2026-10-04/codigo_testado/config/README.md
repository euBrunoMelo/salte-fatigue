# Historical Eye Calibration

The production FATIGUE runtime does not read `eye_calibration.json`. It uses
absolute EAR <= 0.17 with temporal closure and PERCLOS. Existing JSON files are
preserved and ignored; absent or invalid files do not block classification.

The source-only enrollment tools remain for historical experiments. They are
not included in the Docker image and are not required for startup.

Run from the project root, using an environment that contains MediaPipe:

```bash
python3 calibrate_eyes.py --output config/eye_calibration.json
```

Enrollment has explicit open and closed phases. Each eye needs at least 30 valid
samples per phase. Open references use P80, closed references use P20, and both
eyes must have an open/closed separation of at least `0.03` EAR.

These historical references are specific to a camera geometry and mount.
