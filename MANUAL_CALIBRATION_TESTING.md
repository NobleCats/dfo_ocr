# Manual Calibration and Screenshot Testing

This build focuses on window-detection and scale-mismatch fixes only. OCR model
bundling is intentionally unchanged.

## Manual calibration test

1. Run DFO and open the party/raid group request list.
2. Run `DFOGANG_RaidHelper.exe`.
3. Click `AREA`.
4. Align the blue guide with the request-list window.
   - Drag only the small top-right pivot handle to move the guide.
   - Use the scale slider to resize the guide from the top-left pivot.
   - The guide is intentionally faint until the handle is hovered or dragged.
5. Click the play button.

When manual calibration is saved, the app skips automatic party-apply window
detection and directly runs OCR on the calibrated geometry. This should be much
faster and less sensitive to UI scale or marker-detection differences.

The calibration is saved in:

```text
%LOCALAPPDATA%\DFOGANG_RaidHelper\settings.json
```

## Screenshot debug test in the GUI

This is for tester PCs without DFO installed.

1. Run `DFOGANG_RaidHelper.exe`.
2. Click `IMG`.
3. Select a screenshot containing the request-list window.
4. Click `AREA` and align the guide to the request list inside the screenshot.
5. Click the play button.

In screenshot mode the app uses the selected image instead of looking for the
DFO client window. It processes the image once and leaves the result/logs for
inspection.

Logs are written to:

```text
%LOCALAPPDATA%\DFOGANG_RaidHelper\debug.log
```

## Screenshot debug test from source

Use this when Python dependencies are available on the tester machine:

```powershell
C:\Users\Noble\AppData\Local\Programs\Python\Python310\python.exe src\app.py --test-image samples\party_apply_03.png
```

Expected output includes:

```text
[test-image] found=True ...
row=0 fame=...
```

The command exits with code `0` when the request-list detector finds the window,
and `1` when it does not.

## Build

From the repository root:

```powershell
powershell.exe -ExecutionPolicy Bypass -File tools\build_full_onefile_release.ps1 -Python C:\Users\Noble\AppData\Local\Programs\Python\Python310\python.exe -Version v1.0beta
```

The output EXE is written under:

```text
release_dist\
```

Before sending a build to testers, verify both:

```powershell
C:\Users\Noble\AppData\Local\Programs\Python\Python310\python.exe -m py_compile src\capture.py src\party_apply.py src\app.py src\gui_app.py
C:\Users\Noble\AppData\Local\Programs\Python\Python310\python.exe src\app.py --test-image samples\party_apply_03.png
```
