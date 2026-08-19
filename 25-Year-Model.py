from WindSolarBESS import Wind_SolarBESS
import pandas as pd
import numpy as np
import numpy_financial as npf

YEARS = range(26)  # 0 = CAPEX outlay year, 1..25 = operating years


class DashboardStat:
    def __init__(self, gencons,
                 solar_capacity,
                 wind_capacity,
                 BESS_hours,
                 max_SoC_perc,
                 min_SoC_perc,
                 solar_gen_degrad,
                 wind_gen_degrad,
                 BESS_capacity_degrad,
                 RTE_degrad,
                 solar_maintenance,
                 wind_maintenance,
                 BESS_maintenance,
                 costs_escalation,
                 tariff,
                 solar_capex,
                 wind_capex,
                 BESS_capex,
                 RTE=0.85,
                 customer_load_factor=0.45,
                 ):

        self.gencons = gencons  # path to the filled-in Template.xlsx

        self.solar_capacity = solar_capacity  # Grid Allowable for solar
        self.wind_capacity = wind_capacity  # Grid Allowable for wind
        self.customer_RTC = self.solar_capacity + self.wind_capacity  # Total Grid Allowable

        self.BESS_hours = BESS_hours  # Hours of battery life (nameplate, Year 1)

        self.max_SoC_perc = max_SoC_perc  # The maximum we want to charge the battery to (fraction, 0-1)
        self.min_SoC_perc = min_SoC_perc  # The minimum we want to keep the charge at (fraction, 0-1)

        # Defines when we want to be discharging solar and when we want to be discharging BESS

        # Round-Trip Efficiency (How much energy lost to the battery)
        self.RTE = RTE

        # Total BESS Capacity (Year 1 nameplate, before degradation)
        self.battery_capacity = self.BESS_hours * self.solar_capacity

        # DC solar capacity (2 * AC)
        self.solar_capacity_dc = self.solar_capacity * 2

        # Maximum and minimum we want to charge the battery to (Year 1 nameplate, absolute)
        self.max_SoC = self.max_SoC_perc * self.battery_capacity
        self.min_SoC = self.min_SoC_perc * self.battery_capacity

        # ---- Degradation: all four are RETENTION factors applied as
        # factor**year (e.g. 0.995 for a 0.5%/yr fade), matching how
        # RTE_degrad/BESS_capacity_degrad were already being used. ----
        self.solar_degrad = solar_gen_degrad
        self.wind_gen_degrad = wind_gen_degrad
        self.BESS_capacity_degrad = BESS_capacity_degrad
        self.RTE_degrad = RTE_degrad

        # ---- O&M cost rate parameters (Rs per unit capacity/1000, same
        # convention as the CAPEX formula below) -- now actually used in
        # calc_maintenance_cost (see bug #8 above). ----
        self.solar_maintenance = solar_maintenance
        self.wind_maintenance = wind_maintenance
        self.BESS_maintenance = BESS_maintenance
        self.costs_escalation = costs_escalation

        self.solar_capex = solar_capex
        self.wind_capex = wind_capex
        self.BESS_capex = BESS_capex

        self.tariff = tariff
        self.customer_load_factor = customer_load_factor

        self.irr_table = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def gencons_to_df(self):
        """Reads the filled-in Template.xlsx's Generation and Consumption sheets."""
        generation_data = pd.read_excel(
            self.gencons,
            sheet_name="Generation",
            header=3,  # 0-indexed: the header row is Excel row 4
            nrows=8760,
        )
        generation = generation_data[["Month", "Day", "HRS", "Wind (at XMWp)", "Solar (at XMWp)"]]

        # Consumption sheet's header is row 1 (0-indexed row 0), with
        # "Time Block" as the row label -- this is the EXACT shape
        # Wind_SolarBESS.calc_consumption_table() produces on its own, so
        # we use it directly instead of that method's load-factor estimate.
        consumption = pd.read_excel(
            self.gencons,
            sheet_name="Consumption",
            header=0,
            index_col="Time Block",
        )

        # The template's Total/Average cells are meant to be live Excel
        # formulas (the blank template shows Total=0, Average='#DIV/0!'
        # until real numbers are entered and Excel recalculates). Reading
        # them via pandas can pick up stale or blank cached values instead
        # of the true total -- confirmed by testing: NaN Total/Average
        # cells silently NaN out every downstream Effective Replacement
        # calculation. Fixed by recomputing Total/Average ourselves from
        # only the Jan-Dec monthly columns, the same way
        # Wind_SolarBESS.calc_consumption_table() builds its own table.
        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        consumption = consumption.loc[["Normal", "Solar", "Peak"], month_labels]
        consumption["Total"] = consumption.sum(axis=1)

        total_row = consumption.sum(axis=0)
        total_row.name = "Total"
        consumption = pd.concat([consumption, total_row.to_frame().T])

        consumption["Average"] = consumption["Total"] / consumption.loc["Total", "Total"]

        self.generation = generation
        self.consumption = consumption
        return self

    # ------------------------------------------------------------------
    # Per-year rule-based simulation
    # ------------------------------------------------------------------
    def _build_plant_for_year(self, year):
        """Builds a Wind_SolarBESS plant for a given operating year (>=1),
        with all degradation factors applied for that year."""
        BESS_hours = self.BESS_hours * (((100 - self.BESS_capacity_degrad) / 100) ** (year-1))

        # Original script's intentional rule: as usable battery capacity
        # (hours x usable SoC window) shrinks, the solar discharge window
        # needs to end an hour earlier to still be coverable. Kept as-is.
        usable_bess_hours = BESS_hours * (self.max_SoC_perc - self.min_SoC_perc)
        if usable_bess_hours >= 5:
            solar_discharge_end = 14
        else:
            solar_discharge_end = 15

        RTE = self.RTE * (((100 - self.RTE_degrad) / 100) ** (year-1))

        gen = self.generation.copy()
        gen["Wind (at XMWp)"] = self.generation["Wind (at XMWp)"] * (((100 - self.wind_gen_degrad) / 100) ** (year - 1))
        gen["Solar (at XMWp)"] = self.generation["Solar (at XMWp)"] * (((100 - self.solar_degrad) / 100) ** (year - 1))

        plant = Wind_SolarBESS(
            generation=gen,
            customer_load_factor=self.customer_load_factor,
            solar_capacity=self.solar_capacity,
            wind_capacity=self.wind_capacity,
            BESS_hours=BESS_hours,
            max_SoC_perc=self.max_SoC_perc,
            min_SoC_perc=self.min_SoC_perc,
            solar_discharge_start=1,
            solar_discharge_end=solar_discharge_end,
            bess_discharge_start=18,
            bess_discharge_end=24,
            RTE=RTE,
        )
        return plant

    def calc_revenue(self, year):
        """Returns (revenue_RsCr, discharged_MnkWh) for the given year."""
        if year == 0:
            return 0, 0

        plant = self._build_plant_for_year(year)
        effective_replacement, discharged_kwh = plant.run_analytics(consumption_table=self.consumption)
        discharged = discharged_kwh / 1_000_000.0  # kWh -> Mn kWh

        revenue = discharged * self.tariff
        return revenue, discharged

    def calc_maintenance_cost(self, year):
        if year == 0:
            costs = ((
                self.solar_capex * 2 * (self.solar_capacity / 1000)
                + self.wind_capex * (self.wind_capacity / 1000)
                + self.BESS_capex * (self.battery_capacity / 1000)) * 1000
            ) 
            return costs

        else:
            costs = (
                self.BESS_maintenance * self.battery_capacity 
                + self.solar_maintenance * self.solar_capacity_dc 
                + self.wind_maintenance * self.wind_capacity 
            ) * 1.18 / 1000000 + 0.5

            costs = costs * (((100 + self.costs_escalation) / 100) ** (year-1))
            return costs

    # ------------------------------------------------------------------
    # Revenue / Costs / EBITDA table + financial metrics
    # ------------------------------------------------------------------
    def calc_irr_table(self):
        rows = []
        for i in YEARS:
            revenue, discharged = self.calc_revenue(i)
            costs = self.calc_maintenance_cost(i)
            rows.append({
                "Year": i,
                "Discharged_MnkWh": discharged,
                "Revenue_RsCr": revenue,
                "Costs_RsCr": costs,
                "EBITDA_RsCr": revenue - costs,
            })
        self.irr_table = pd.DataFrame(rows)
        return self.irr_table

    def calc_irr(self):
        if self.irr_table is None:
            self.calc_irr_table()
        return npf.irr(self.irr_table["EBITDA_RsCr"])

    def calc_capex_ebitda_ratio(self):
        """
        Capex/EBITDA = Year 0's CAPEX outlay / the AVERAGE EBITDA across the
        25 operating years (Years 1-25) -- per your description ("average
        ebitda after the original cost over the 25 years of revenue
        generation"), not Year 1's EBITDA alone.
        """
        if self.irr_table is None:
            self.calc_irr_table()

        capex = self.irr_table.loc[self.irr_table["Year"] == 0, "Costs_RsCr"].iloc[0]
        avg_ebitda = self.irr_table.loc[self.irr_table["Year"] >= 1, "EBITDA_RsCr"].mean()
        return capex / avg_ebitda

    def save_irr_table(self, path: str = "Revenue_Costs_EBITDA_Table.xlsx"):
        """Saves the Year/Discharged/Revenue/Costs/EBITDA table to Excel."""
        if self.irr_table is None:
            self.calc_irr_table()
        self.irr_table.to_excel(path, sheet_name="Revenue_Costs_EBITDA", index=False)
        return path

    # ------------------------------------------------------------------
    # Effective Replacement, with vs. without BESS
    # ------------------------------------------------------------------
    def calc_effective_replacement(self, year=1):
        """
        Effective Replacement for a given operating year (defaults to Year
        1, i.e. no degradation yet), WITH the battery configured as given.
        """
        plant = self._build_plant_for_year(year)
        effective_replacement, _ = plant.run_analytics(consumption_table=self.consumption)
        return effective_replacement

    def calc_effective_replacement_no_bess(self, year=1):
        """
        Same simulation, same year, same degradation -- but with BESS_hours
        forced to 0 (no battery at all), so the ONLY thing that changes is
        whether a battery exists. This isolates exactly how much of the
        Effective Replacement figure is attributable to the battery.
        """
        RTE = self.RTE * (((100 - self.RTE_degrad) / 100) ** (year-1))

        gen = self.generation.copy()
        gen["Wind (at XMWp)"] = self.generation["Wind (at XMWp)"] * (((100 - self.wind_gen_degrad) / 100) ** year)
        gen["Solar (at XMWp)"] = self.generation["Solar (at XMWp)"] * (((100 - self.solar_degrad) / 100) ** year)

        plant_no_bess = Wind_SolarBESS(
            generation=gen,
            customer_load_factor=self.customer_load_factor,
            solar_capacity=self.solar_capacity,
            wind_capacity=self.wind_capacity,
            BESS_hours=0,  # <-- the only thing that changes vs. calc_effective_replacement
            max_SoC_perc=self.max_SoC_perc,
            min_SoC_perc=self.min_SoC_perc,
            solar_discharge_start=1,
            solar_discharge_end=14,
            bess_discharge_start=18,
            bess_discharge_end=24,
            RTE=RTE,
        )
        effective_replacement, _ = plant_no_bess.run_analytics(consumption_table=self.consumption)
        return effective_replacement

    def effective_replacement_comparison(self, year=1) -> dict:
        with_bess = self.calc_effective_replacement(year)
        without_bess = self.calc_effective_replacement_no_bess(year)
        return {
            "Year": year,
            "Effective_Replacement_With_BESS": with_bess,
            "Effective_Replacement_Without_BESS": without_bess,
            "Uplift_From_BESS": with_bess - without_bess,
        }

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def run_dashboard(self, irr_table_path: str = "Revenue_Costs_EBITDA_Table.xlsx") -> dict:
        """
        Runs the full pipeline: loads data, builds the 25-year Revenue/
        Costs/EBITDA table (and saves it to Excel), and computes IRR,
        Capex/EBITDA, and the Effective Replacement with/without BESS
        comparison. Returns everything as a single dict for the dashboard
        UI to consume directly.
        """
        self.gencons_to_df()
        self.calc_irr_table()
        self.save_irr_table(irr_table_path)

        irr = self.calc_irr()
        capex_ebitda = self.calc_capex_ebitda_ratio()
        replacement = self.effective_replacement_comparison(year=1)

        return {
            "irr_table": self.irr_table,
            "irr": irr,
            "capex_to_ebitda_ratio": capex_ebitda,
            **replacement,
        }


if __name__ == "__main__":
    dash = DashboardStat(
        gencons="Template.xlsx",
        solar_capacity=150,
        wind_capacity=49.5,
        BESS_hours=6,
        max_SoC_perc=1.0,
        min_SoC_perc=0.0,
        solar_gen_degrad=0.5,      # 0.5%/yr solar generation fade
        wind_gen_degrad=0.2,       # 0.2%/yr wind generation fade
        BESS_capacity_degrad=2,   # 2%/yr BESS capacity fade
        RTE_degrad=0.1,            # 0.1%/yr RTE fade
        solar_maintenance=500000,    # Rs, matches the original hardcoded (500000+127100)
        wind_maintenance=910000,    # Rs, matches the original hardcoded (910000+127100)
        BESS_maintenance=100000,     # Rs, matches the original hardcoded value
        costs_escalation=3,       # 3%/yr O&M escalation
        tariff=6.0,                  # Rs/kWh (flat, matches earlier IRR-sheet convention)
        solar_capex=40,
        wind_capex=90,
        BESS_capex=10,
        RTE=0.85,
    )

    results = dash.run_dashboard()

    print("IRR:", results["irr"])
    print("Capex/EBITDA (avg over 25 yrs):", results["capex_to_ebitda_ratio"])
    print("Effective Replacement WITH BESS (Year 1):", results["Effective_Replacement_With_BESS"])
    print("Effective Replacement WITHOUT BESS (Year 1):", results["Effective_Replacement_Without_BESS"])
    print("Uplift from BESS:", results["Uplift_From_BESS"])
    print("\nRevenue/Costs/EBITDA table:")
    print(results["irr_table"].to_string(index=False))