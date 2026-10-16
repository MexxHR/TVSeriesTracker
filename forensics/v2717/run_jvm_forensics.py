"""Compile actual frozen Kotlin paths with JVM type doubles; never build an APK.

Requires local cached Kotlin 2.0.21/library jars and Android 35 SDK android.jar.
No dependency downloads. Run from repository root with --java/--android-jar
if JAVA_HOME and ANDROID_HOME are not configured.
"""
from pathlib import Path
import argparse,os,subprocess,sys

def main():
 p=argparse.ArgumentParser();p.add_argument('--java');p.add_argument('--android-jar');p.add_argument('--gradle-cache',type=Path,default=Path.home()/'.gradle/caches/modules-2/files-2.1');args=p.parse_args()
 root=Path(__file__).resolve().parents[2];folder=root/'.validation-temp/v2717-jvm';folder.mkdir(parents=True,exist_ok=True);out=folder/'classes';out.mkdir(exist_ok=True)
 java=args.java or str(Path(os.environ.get('JAVA_HOME',''))/'bin'/('java.exe' if os.name=='nt' else 'java'))
 sdk=args.android_jar or str(Path(os.environ.get('ANDROID_HOME',str(Path.home()/'AppData/Local/Android/Sdk')))/'platforms/android-35/android.jar')
 def jar(group,artifact,version):
  matches=sorted((args.gradle_cache/group/artifact/version).glob(f'*/{artifact}-{version}.jar'))
  if not matches:raise RuntimeError('UNAVAILABLE: cached '+artifact)
  return str(matches[0])
 coordinates=[('org.jetbrains.kotlin','kotlin-compiler-embeddable','2.0.21'),('org.jetbrains.kotlin','kotlin-stdlib','2.0.21'),('org.jetbrains.kotlin','kotlin-script-runtime','2.0.21'),('org.jetbrains.kotlin','kotlin-reflect','1.6.10'),('org.jetbrains.intellij.deps','trove4j','1.0.20200330'),('org.jetbrains.kotlinx','kotlinx-coroutines-core-jvm','1.9.0'),('org.jetbrains','annotations','23.0.0'),('com.google.code.gson','gson','2.11.0'),('junit','junit','4.13.2'),('org.hamcrest','hamcrest-core','1.3'),('com.squareup.okhttp3','okhttp','4.12.0'),('com.squareup.okio','okio-jvm','3.9.0')]
 jars={a:jar(g,a,v) for g,a,v in coordinates}
 compiler=os.pathsep.join(jars[k] for k in ['kotlin-compiler-embeddable','kotlin-stdlib','kotlin-script-runtime','kotlin-reflect','trove4j','kotlinx-coroutines-core-jvm','annotations'])
 libs=[jars[k] for k in ['kotlin-stdlib','kotlinx-coroutines-core-jvm','annotations','gson','junit','hamcrest-core','okhttp','okio-jvm']]
 src=root/'app/src/main/java/com/example/tvseriestracker/data'
 sources=[src/n for n in ['Series.kt','OfficialData.kt','OfficialDataRepository.kt','RequestedOfficialRefresh.kt','MonitoringTracking.kt']]
 sources += [src/'MonitoringObligations.kt',src/'remote/MonitoringProtocol.kt']
 sources+=sorted((root/'forensics/v2717').glob('*.kt'))
 sources+=[root/'app/src/test/java/com/example/tvseriestracker/data'/n for n in ['V2713OfficialRefreshTest.kt','OfficialDataTest.kt']]
 cmd=[java,'-cp',compiler,'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler','-no-jdk','-no-reflect','-no-stdlib','-jvm-target','17','-classpath',os.pathsep.join(libs+[sdk]),'-d',str(out)]+list(map(str,sources))
 result=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace');(folder/'compile.log').write_text(result.stdout+result.stderr,encoding='utf-8')
 if result.returncode:print(result.stdout+result.stderr);return result.returncode
 classes=['V2713OfficialRefreshTest','OfficialDataTest','V2717E2EForensicsTest','RequestPathForensicsTest']
 result=subprocess.run([java,'-cp',os.pathsep.join([str(out)]+libs+[sdk]),'org.junit.runner.JUnitCore']+['com.example.tvseriestracker.data.'+c for c in classes],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=45)
 (folder/'tests.log').write_text(result.stdout+result.stderr,encoding='utf-8');print(result.stdout+result.stderr);return result.returncode
if __name__=='__main__':sys.exit(main())
