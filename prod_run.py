from cm_power_merge.cli import main

if __name__ == "__main__":
    import sys

    print(sys.executable)

    #initial
    # main([
    #     "--config", r"C:\Users\jeremy.suwinski\Documents\Projects\CM_PowerMerge\config.toml",
    #     "run",
    #     "--month", "2026-09",
    #     "--pm-file", r"C:\Users\jeremy.suwinski\TierPoint, LLC\Enterprise Data & Analytics - Documents\Projects\PowerMerge\output\PM Power Commit Data.xlsx",
    #
    #
    # ])

    # ongoing:
    main([
        "--config", r"C:\Users\jeremy.suwinski\Documents\Projects\CM_PowerMerge\config.toml",
        "run"
    ])