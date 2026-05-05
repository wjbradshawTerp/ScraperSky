from config import settings
from scraper import SCRAPER_REGISTRY

VALID_MODES = {"home", "follows"}


def validate_mode(mode):
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}. Must be one of {VALID_MODES}.")


def main():
    validate_mode(settings.MODE)
    scraper_class = SCRAPER_REGISTRY.get(settings.PLATFORM)
    if not scraper_class:
        raise ValueError(f"Unsupported platform: {settings.PLATFORM}")

    scraper = scraper_class(
        mode=settings.MODE,
    )

    scraper.run()


if __name__ == "__main__":
    main()
