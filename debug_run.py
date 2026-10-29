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
    # cfg = SqlConfig
    # stg, prod = cfg.staging_table, cfg.production_table
    # prod_db = cfg.production_database
    # sl.quote_table(prod)
    #
    # with contextlib.closing(sl.connect(cfg= SqlConfig, autocommit=False)) as conn:
    #     cur = conn.cursor()
    #     try:
    #         cur.fast_executemany = True
    #         cur.execute(f"SELECT TOP 1 * FROM eda_lake.xlsx_extract.tbl__xlsx_m_colo_kw_audit")
    #         loaded = cur.fetchall()
    #         print(loaded)
    #     except Exception:
    #         conn.rollback()
    #         raise
