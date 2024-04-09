import tomllib

# Load the configuration file
try:
    with open("./data/config.toml", "rb") as f:
        config = tomllib.load(f)
except FileNotFoundError:
    print("Configuration file not found. Create a config.toml file in the root directory.")
    exit(1)
