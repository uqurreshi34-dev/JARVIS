# PlatformIO, before the build (extra_scripts "pre:"): the toy's "Jarvis" wake word model
# (wake/srmodels.bin, made by `python tools/toy_wake_model.py`) goes on with every upload, into
# the "model" partition of toy_partitions.csv. Without the file, nothing is added, and the toy
# says so when it starts: every recording then goes to be checked for "Jarvis" as before.
from os.path import isfile, join

Import("env")  # noqa: F821 - given by PlatformIO

MODEL_OFFSET = "0xef0000"
model = join(env.subst("$PROJECT_DIR"), "wake", "srmodels.bin")  # noqa: F821

if isfile(model):
    env.Append(FLASH_EXTRA_IMAGES=[(MODEL_OFFSET, model)])  # noqa: F821
else:
    print("toy_wake_image.py: no wake/srmodels.bin (python tools/toy_wake_model.py makes it); "
          "the toy will send every recording to be checked for Jarvis")
