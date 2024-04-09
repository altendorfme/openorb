import tomllib


def load_config():
    try:
        with open("./data/config.toml", "rb") as f:
            config = tomllib.load(f)
            return config
    except FileNotFoundError:
        print(
            "Configuration file not found. Create a config.toml file in the root directory.")
        exit(1)
