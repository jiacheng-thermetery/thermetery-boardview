Thermetery Boardviewer - portable Windows x64 edition

Extract the ENTIRE ZIP first, then run ThermeteryBoardviewer.exe from the
extracted folder. Copying only ThermeteryBoardviewer.exe out of the ZIP
fails with "Failed to load Python DLL ... python314.dll": the _internal
directory sitting next to the .exe IS the Python runtime, and the program
cannot start without it.

Keep this whole directory together and run ThermeteryBoardviewer.exe.
The _internal directory and portable.flag are required parts of the app.

Settings and remembered decryption keys are stored in the adjacent data
directory as plaintext local files. Official downloads never include keys,
but your copy will: remove data\private before sharing or re-zipping the folder.
You may move or back up the extracted folder as one unit. Do not run the
application inside the ZIP; extract it to a writable directory first.

Open-source license notices are under licenses.
