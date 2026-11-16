"""Build the Android webpage-to-WeChat test app with official Android tools and a JDK."""

import argparse
import os
import shutil
import subprocess
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jdk', type=Path, required=True)
    parser.add_argument('--build-tools', type=Path, required=True)
    parser.add_argument('--android-jar', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    work = root / '.build'
    classes, dex = work / 'classes', work / 'dex'
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(exist_ok=True)
    ext = '.exe' if os.name == 'nt' else ''
    java = args.jdk / 'bin' / ('java' + ext)
    def run(*command):
        subprocess.run([str(v) for v in command], check=True)
    sources = sorted((root / 'src').rglob('*.java'))
    run(args.jdk / 'bin' / ('javac' + ext), '--release', '8', '-encoding', 'UTF-8',
        '-classpath', args.android_jar, '-d', classes, *sources)
    archive = work / 'classes.jar'
    run(args.jdk / 'bin' / ('jar' + ext), 'cf', archive, '-C', classes, '.')
    run(java, '-cp', args.build_tools / 'lib/d8.jar', 'com.android.tools.r8.D8',
        '--min-api', '26', '--lib', args.android_jar, '--output', dex, archive)
    unsigned, aligned = work / 'unsigned.apk', work / 'aligned.apk'
    assets = work / 'assets'
    shutil.copytree(root.parents[1] / 'dist', assets / 'web', dirs_exist_ok=True)
    shutil.copytree(root / 'assets', assets, dirs_exist_ok=True)
    run(args.build_tools / ('aapt2' + ext), 'link', '-o', unsigned,
        '-I', args.android_jar, '--manifest', root / 'AndroidManifest.xml', '-A', assets)
    packed = work / 'packed.apk'
    with zipfile.ZipFile(unsigned) as original, zipfile.ZipFile(packed, 'w') as package:
        for item in original.infolist():
            package.writestr(item.filename, original.read(item), compress_type=item.compress_type)
        package.write(dex / 'classes.dex', 'classes.dex')
    run(args.build_tools / ('zipalign' + ext), '-f', '4', packed, aligned)
    key = work / 'test-only.jks'
    if not key.exists():
        run(args.jdk / 'bin' / ('keytool' + ext), '-genkeypair', '-keystore', key,
            '-alias', 'test', '-storepass', 'android', '-keypass', 'android',
            '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650',
            '-dname', 'CN=Framekeep Local Test')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    signer = args.build_tools / 'lib/apksigner.jar'
    run(java, '-jar', signer, 'sign', '--ks', key, '--ks-key-alias', 'test',
        '--ks-pass', 'pass:android', '--out', args.output, aligned)
    run(java, '-jar', signer, 'verify', '--verbose', '--print-certs', args.output)
    run(args.build_tools / ('aapt2' + ext), 'dump', 'permissions', args.output)


if __name__ == '__main__':
    main()
