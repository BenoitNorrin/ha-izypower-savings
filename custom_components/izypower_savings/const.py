"""Shared integration identifiers."""

DOMAIN = "izypower_savings"
NAME = "Izypower Savings"
VERSION = "0.1.0"
SIGNAL = f"{DOMAIN}_updated"
DEFAULT_MAX_AGE = 900
DEFAULT_MAX_GAP = 900
PERIODS = ("jour", "mois", "annee")

# One household per Home Assistant instance; the sample dashboard is reusable.
STATISTICS = (
    ("gain_eur", "gain", "Gain total estimé"),
    ("economie_eur", "economie", "Économies sur les achats estimées"),
    ("revenus_eur", "revenus", "Revenus d’injection estimés"),
    ("avec_eur", "avec", "Coût net estimé avec installation"),
    ("sans_eur", "sans", "Coût estimé sans installation"),
)
ENTRY_MINOR_VERSION = 2
