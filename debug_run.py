from cm_power_merge.cli import main



if __name__ == "__main__":
    # main([
    #     "--config", "config.toml",
    #     "compare",
    #     "--new", r"C:\Users\jeremy.suwinski\OneDrive - TierPoint, LLC\Documents\temp\CM Power Merged 2026-09.xlsx",
    #     "--ref", r"C:\Users\jeremy.suwinski\OneDrive - TierPoint, LLC\Documents\temp\PM Power Commit Data.xlsx",
    #     "--key", "Service Asset Number",
    #
    #
    # ])

    main([
        "--config", "config.toml",
        "run",
        "--month", "2026-09",
        "--pm-file", r"C:\Users\jeremy.suwinski\OneDrive - TierPoint, LLC\Documents\temp\PM Power Commit Data.xlsx",
        # Can comment out pm-file after intial output is ran

    ])