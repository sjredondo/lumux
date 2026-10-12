import os
import tempfile

# Keep tests away from the real ~/.config/lumux
os.environ["HOME"] = tempfile.mkdtemp(prefix="lumux-test-home")
