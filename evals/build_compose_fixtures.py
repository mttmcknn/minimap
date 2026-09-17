#!/usr/bin/env python3
"""Build pinned Compose fixtures in a new, isolated clone; never edit the source repo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

REVISION = "d3ff757b289f7036815978a8f7b16706ee3423b0"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local compose-samples checkout")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--git", default="git")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    clone = output / "compose-samples"
    subprocess.run([args.git, "clone", "--no-hardlinks", str(args.source.resolve()), str(clone)], check=True)
    subprocess.run([args.git, "-C", str(clone), "checkout", "--detach", REVISION], check=True)
    apks = output / "apks"
    apks.mkdir()
    manifest = {"revision": REVISION, "fixtures": {}, "java_home": os.getenv("JAVA_HOME")}

    def build(sample, variant, changes=None):
        name = f"{sample.lower()}-{variant}"
        with (output / f"{name}.build.log").open("w") as log:
            subprocess.run(["./gradlew", ":app:assembleDebug", "--no-daemon", "--console=plain"],
                           cwd=clone / sample, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        apk = apks / f"{name}.apk"
        shutil.copy2(clone / sample / "app/build/outputs/apk/debug/app-debug.apk", apk)
        manifest["fixtures"][name] = {"sha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "changes": changes or {}}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(name, manifest["fixtures"][name]["sha256"], flush=True)

    build("Jetsnack", "baseline")
    build("JetNews", "baseline")
    build("Jetchat", "baseline")
    strings = clone / "Jetsnack/app/src/main/res/values/strings.xml"
    home = clone / "Jetsnack/app/src/main/java/com/example/jetsnack/ui/home/Home.kt"
    original_strings, original_home = strings.read_text(), home.read_text()

    def replace(text, before, after):
        assert text.count(before) == 1, f"Fixture source drift: {before}"
        return text.replace(before, after)

    try:
        strings.write_text(replace(original_strings, '<string name="home_search">Search</string>', '<string name="home_search">Discover</string>'))
        build("Jetsnack", "renamed", {str(strings.relative_to(clone)): "home_search: Search → Discover; route unchanged"})
        strings.write_text(original_strings)
        relocated = replace(original_home, "val routes = remember { tabs.map { it.route } }",
                            "val visibleTabs = tabs.filter { it != HomeSections.SEARCH || currentRoute == HomeSections.PROFILE.route || currentRoute == HomeSections.SEARCH.route }\n    val routes = visibleTabs.map { it.route }")
        relocated = replace(relocated, "selectedIndex = currentSection.ordinal,", "selectedIndex = visibleTabs.indexOf(currentSection),")
        relocated = replace(relocated, "tabs.forEach { section ->", "visibleTabs.forEach { section ->")
        home.write_text(relocated)
        build("Jetsnack", "relocated", {str(home.relative_to(clone)): "Search tab visible from Profile or Search; layout count/index use visible tabs"})
        home.write_text(replace(original_home, "onSelected = { navigateToRoute(section.route) },",
                                "onSelected = { navigateToRoute(if (section == HomeSections.SEARCH) HomeSections.CART.route else section.route) },"))
        build("Jetsnack", "broken", {str(home.relative_to(clone)): "Search callback deliberately navigates to Cart"})
    finally:
        strings.write_text(original_strings)
        home.write_text(original_home)


if __name__ == "__main__":
    main()
