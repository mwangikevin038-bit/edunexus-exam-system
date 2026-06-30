const { TwaManifest, TwaGenerator, AndroidSdkTools, JdkHelper, KeyTool, GradleWrapper, JarSigner, ConsoleLog, BufferedLog } = require('@bubblewrap/core');
const path = require('path');
const fs = require('fs');

const TARGET_DIR = path.resolve(__dirname);
const MANIFEST_FILE = path.join(TARGET_DIR, 'twa-manifest.json');
const SDK_DIR = path.join(process.env.USERPROFILE, '.bubblewrap', 'android_sdk');
const JDK_PATH = 'C:\\Program Files\\Microsoft\\jdk-17.0.19.10-hotspot';

async function main() {
  console.log('=== EduNexus APK Builder ===\n');

  // 1. Load config
  const config = { jdkPath: JDK_PATH, androidSdkPath: SDK_DIR };
  console.log('Config loaded:', config);

  // 2. Load TWA manifest
  console.log('Loading twa-manifest.json...');
  const twaManifest = await TwaManifest.fromFile(MANIFEST_FILE);
  console.log('Manifest loaded:', twaManifest.name, twaManifest.packageId);

  // 3. Generate TWA project
  console.log('\nGenerating Android project...');
  const twaGenerator = new TwaGenerator();
  const log = new BufferedLog(new ConsoleLog('TWA'));
  await twaGenerator.createTwaProject(TARGET_DIR, twaManifest, log);
  log.flush();
  console.log('Project generated successfully.');

  // 4. Generate checksum
  const crypto = require('crypto');
  const manifestContents = await fs.promises.readFile(MANIFEST_FILE);
  const checksum = crypto.createHash('sha1').update(manifestContents).digest('hex');
  await fs.promises.writeFile(path.join(TARGET_DIR, 'manifest-checksum.txt'), checksum);
  console.log('Checksum generated.');

  // 5. Set up SDK tools
  console.log('\nSetting up Android SDK tools...');
  const processObj = process;
  const jdkHelper = new JdkHelper(processObj, config);
  const androidSdkTools = await AndroidSdkTools.create(processObj, config, jdkHelper, log);

  // 6. Install build tools if needed
  if (!await androidSdkTools.checkBuildTools()) {
    console.log('Installing build tools...');
    await androidSdkTools.installBuildTools();
  }
  console.log('Build tools ready.');

  // 7. Build APK
  console.log('\nBuilding APK...');
  const gradleWrapper = new GradleWrapper(processObj, androidSdkTools);
  await gradleWrapper.assembleRelease();
  console.log('APK assembled.');

  // 8. Zipalign
  const APK_UNSIGNED = './app/build/outputs/apk/release/app-release-unsigned.apk';
  const APK_ALIGNED = './app-release-unsigned-aligned.apk';
  await androidSdkTools.zipalignOnlyVerification(APK_UNSIGNED);
  fs.copyFileSync(APK_UNSIGNED, APK_ALIGNED);
  console.log('APK aligned.');

  // 9. Sign APK
  const APK_SIGNED = './app-release-signed.apk';
  const keyTool = new KeyTool(jdkHelper, log);
  await androidSdkTools.apksigner(
    path.resolve(TARGET_DIR, 'android-keystore.jks'),
    '"edunexus123"',
    'edunexus',
    '"edunexus123"',
    APK_ALIGNED,
    APK_SIGNED
  );
  console.log('\n=== BUILD COMPLETE ===');
  console.log('Signed APK:', path.resolve(TARGET_DIR, APK_SIGNED));

  // 10. Also build app bundle
  console.log('\nBuilding App Bundle...');
  await gradleWrapper.bundleRelease();
  const jarSigner = new JarSigner(jdkHelper);
  const AAB_UNSIGNED = './app/build/outputs/bundle/release/app-release.aab';
  const AAB_SIGNED = './app-release-bundle.aab';
  await jarSigner.sign(
    { path: path.resolve(TARGET_DIR, 'android-keystore.jks'), alias: 'edunexus' },
    '"edunexus123"',
    '"edunexus123"',
    AAB_UNSIGNED,
    AAB_SIGNED
  );
  console.log('Signed AAB:', path.resolve(TARGET_DIR, AAB_SIGNED));
  console.log('\n=== ALL DONE ===');
}

main().catch(err => {
  console.error('BUILD FAILED:', err);
  process.exit(1);
});
