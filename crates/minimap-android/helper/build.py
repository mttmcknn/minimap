#!/usr/bin/env python3
"""Rebuild the small bundled observation helper with JDK 21 and Android SDK 36.

Normal Cargo builds use the checked-in JAR and need neither Java nor an SDK.
The JAR contains only our compiled class; no Android framework code is bundled.
"""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=os.environ.get("ANDROID_HOME"))
    parser.add_argument("--jdk", type=Path, default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.sdk or not args.jdk:
        parser.error("Provide --sdk and --jdk, or ANDROID_HOME and JAVA_HOME")
    root = Path(__file__).resolve().parent
    env = dict(os.environ, JAVA_HOME=str(args.jdk))
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        subprocess.run([str(args.jdk / "bin/javac"), "-source", "8", "-target", "8", "-g:none",
                        "-Xlint:-options", "-classpath", str(args.sdk / "platforms/android-36/android.jar"),
                        "-d", str(work), str(root / "MinimapLayout.java")], check=True, env=env)
        subprocess.run([str(args.sdk / "build-tools/36.0.0/d8"), "--min-api", "23", "--output", str(work),
                        str(work / "dev/minimap/MinimapLayout.class")], check=True, env=env)
        jar = work / "minimap-layout.jar"
        with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_STORED) as archive:
            entry = zipfile.ZipInfo("classes.dex", (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, (work / "classes.dex").read_bytes())
        payload = jar.read_bytes()
        destination = root / "minimap-layout.jar"
        if args.check:
            if destination.read_bytes() != payload:
                raise SystemExit("Bundled layout helper differs from rebuilt source")
        else:
            destination.write_bytes(payload)
        print(hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    main()
