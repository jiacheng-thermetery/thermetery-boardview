project(
  'thermetery-boardviewer',
  'c',
  version : '1.0.0',
  default_options : [
    'warning_level=3',
    'c_std=c11',
    'buildtype=release'
  ],
)

# MinGW applies the Unix-style `lib` prefix to Windows DLLs by default,
# while the Python loaders and release layout use `tvw_native.dll`, etc.
# Keep the normal `lib` prefix on Unix-like platforms only.
native_name_prefix = host_machine.system() == 'windows' ? '' : 'lib'

tvw_native_lib = shared_library(
  'tvw_native',
  'src/parsers/native/tvw_native.c',
  name_prefix : native_name_prefix,
  install : false,
)

xzz_native_lib = shared_library(
  'xzz_native',
  'src/parsers/native/xzz_native.c',
  name_prefix : native_name_prefix,
  install : false,
)

rc6_native_lib = shared_library(
  'rc6_native',
  'src/parsers/native/rc6_native.c',
  name_prefix : native_name_prefix,
  install : false,
)

# the following is hacky and intended to maintain functionallyity  
# closer to the original setup with just a script to compile
# it should be reworked such that the python load fetches 
# from the build folder for development, and somewhere else
# for release

py = find_program('python3')
native_dir = meson.project_source_root() / 'src' / 'parsers' / 'native'

copy_tvw = custom_target(
  'copy_tvw_native',
  input : tvw_native_lib,
  output : '@BASENAME@',
  command : [
    py,
    '-c',
    'import shutil,sys; shutil.copy2(sys.argv[1], sys.argv[2])',
    '@INPUT@',
    native_dir / '@INPUT@',
  ],
  build_by_default : true,
)

copy_xzz = custom_target(
  'copy_xzz_native',
  input : xzz_native_lib,
  output : '@BASENAME@',
  command : [
    py,
    '-c',
    'import shutil,sys; shutil.copy2(sys.argv[1], sys.argv[2])',
    '@INPUT@',
    native_dir / '@INPUT@',
  ],
  build_by_default : true,
)

copy_rc6 = custom_target(
  'copy_rc6_native',
  input : rc6_native_lib,
  output : '@BASENAME@',
  command : [
    py,
    '-c',
    'import shutil,sys; shutil.copy2(sys.argv[1], sys.argv[2])',
    '@INPUT@',
    native_dir / '@INPUT@'
  ],
  build_by_default : true,
)
