"""Entry point: python -m murmur [--background | --install-shortcuts | --uninstall-shortcuts]."""
import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
