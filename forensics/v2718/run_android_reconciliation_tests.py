"""Offline cached Kotlin/JVM validation for Phase2.
Compiles all non-UI Android production sources with real cached AndroidX libraries.
BuildConfig/Log are test doubles; no APK/resources/KAPT/device WorkManager engine.
Requires existing local Kotlin2.0.21/Android35 caches and Android Studio JBR.
"""
from pathlib import Path
import os,subprocess,zipfile,json
root=Path(__file__).resolve().parents[2];cache=Path.home()/'.gradle/caches/modules-2/files-2.1';out=root/'.validation-temp/v2718-phase2-jvm';out.mkdir(exist_ok=True);libout=out/'libs';libout.mkdir(exist_ok=True)
def jar(g,a,v):return next((cache/g/a/v).glob('*/'+a+'-'+v+'.jar'))
java=r'C:\Program Files\Android\Android Studio\jbr\bin\java.exe'
compiler=[jar(g,a,v) for g,a,v in [('org.jetbrains.kotlin','kotlin-compiler-embeddable','2.0.21'),('org.jetbrains.kotlin','kotlin-stdlib','2.0.21'),('org.jetbrains.kotlin','kotlin-script-runtime','2.0.21'),('org.jetbrains.kotlin','kotlin-reflect','1.6.10'),('org.jetbrains.intellij.deps','trove4j','1.0.20200330'),('org.jetbrains.kotlinx','kotlinx-coroutines-core-jvm','1.9.0'),('org.jetbrains','annotations','23.0.0')]]
libs=[jar(g,a,v) for g,a,v in [('org.jetbrains.kotlin','kotlin-stdlib','2.0.21'),('org.jetbrains.kotlinx','kotlinx-coroutines-core-jvm','1.9.0'),('org.jetbrains','annotations','23.0.0'),('com.google.code.gson','gson','2.11.0'),('junit','junit','4.13.2'),('org.hamcrest','hamcrest-core','1.3'),('com.squareup.okhttp3','okhttp','4.12.0'),('com.squareup.okio','okio-jvm','3.9.0'),('com.google.guava','listenablefuture','1.0')]]
groups=['androidx.datastore','androidx.work','androidx.lifecycle','androidx.arch.core','androidx.savedstate','androidx.annotation','androidx.concurrent','androidx.collection','androidx.room','androidx.sqlite','androidx.core','com.squareup.retrofit2']
for group in groups:
 for file in (cache/group).rglob('*'):
  if not file.is_file() or file.name.endswith(('-sources.jar','-javadoc.jar')):continue
  if file.suffix=='.jar':libs.append(file)
  if file.suffix=='.aar':
   with zipfile.ZipFile(file) as z:
    if 'classes.jar' in z.namelist():
     target=libout/(group+'-'+file.parent.name+'.jar');target.write_bytes(z.read('classes.jar'));libs.append(target)
sdk=Path.home()/'AppData/Local/Android/Sdk/platforms/android-35/android.jar';libs.append(sdk)
sources=[p for p in (root/'app/src/main/java').rglob('*.kt') if p.name!='MainActivity.kt']
sources += [root/'forensics/v2717/JvmBuildConfig.kt',root/'forensics/v2717/JvmLog.kt']
selected=['MonitoringReconciliationTest.kt','MonitoringDataStoreTest.kt','MonitoringProtocolTest.kt','OfficialDataTest.kt','V2713OfficialRefreshTest.kt','MonitoringTrackingTest.kt']
sources += list((root/'app/src/test/java').rglob('*.kt'))
args=['-no-jdk','-no-reflect','-no-stdlib','-jvm-target','17','-classpath',os.pathsep.join(map(str,libs)),'-d',str(out/'classes')]+list(map(str,sources))
argfile=out/'compile.args';argfile.write_text('\n'.join('"'+a.replace('\\','/')+'"' for a in args),encoding='utf-8')
result=subprocess.run([java,'-cp',os.pathsep.join(map(str,compiler)),'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler','@'+str(argfile)],capture_output=True,text=True,encoding='utf-8',errors='replace');(out/'compile.log').write_text(result.stdout+result.stderr,encoding='utf-8');print(result.stdout+result.stderr)
if result.returncode:raise SystemExit(result.returncode)
classes=['com.example.tvseriestracker.data.'+c for c in ['MonitoringReconciliationTest','MonitoringDataStoreTest','OfficialDataTest','V2713OfficialRefreshTest','MonitoringTrackingTest']]+['com.example.tvseriestracker.data.remote.MonitoringProtocolTest','com.example.tvseriestracker.SearchFailurePhaseTest']
(out/'tmp').mkdir(exist_ok=True)
result=subprocess.run([java,'-Djava.io.tmpdir='+str(out/'tmp'),'-cp',os.pathsep.join(map(str,[out/'classes']+libs)),'org.junit.runner.JUnitCore']+classes,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120);(out/'tests.log').write_text(result.stdout+result.stderr,encoding='utf-8');print(result.stdout+result.stderr);raise SystemExit(result.returncode)
