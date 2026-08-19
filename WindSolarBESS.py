import pandas as pd
import numpy as np


class Wind_SolarBESS:

    def __init__(self, generation, 
                 customer_load_factor,
                 solar_capacity, wind_capacity,
                 BESS_hours,
                 max_SoC_perc, min_SoC_perc,
                 solar_discharge_start, solar_discharge_end,
                 bess_discharge_start, bess_discharge_end,
                 RTE=0.85):

        self.customer_load_factor = customer_load_factor # Customer Load Factor to calculate the consumption
        self.generation = generation.copy() # DataFrame containing the solar and wind generation organised by month and year

        self.solar_capacity = solar_capacity # Grid Allowable for solar
        self.wind_capacity = wind_capacity # Grid Allowable for wind
        self.customer_RTC = self.solar_capacity + self.wind_capacity # Total Grid Allowable

        self.BESS_hours = BESS_hours # Hours of battery life

        self.max_SoC_perc = max_SoC_perc # The maximum we want to charge the battery (to preserve battery health)
        self.min_SoC_perc = min_SoC_perc # The minimum we want to keep the charge at (to preserve battery health)

        # Defines when we want to be discharging solar and when we want to be discharging BESS
        self.solar_discharge_start = solar_discharge_start 
        self.solar_discharge_end = solar_discharge_end
        self.bess_discharge_start = bess_discharge_start
        self.bess_discharge_end = bess_discharge_end

        # Round-Trip Efficiency (How much energy lost to the battery)
        self.RTE = RTE

        # Total BESS Capacity
        self.battery_capacity = self.BESS_hours * self.solar_capacity

        # DC solar capacity (2 * AC)
        self.solar_capacity_dc = self.solar_capacity * 2

        # Maximum and minimum we want to charge the battery to
        self.max_SoC = self.max_SoC_perc * self.battery_capacity
        self.min_SoC = self.min_SoC_perc * self.battery_capacity                 


    def calc_total_gen(self):
        """
        Adds together the total solar generation and wind generation.
        """

        self.generation["Total Generation"] = (
            self.generation["Wind (at XMWp)"] + self.generation["Solar (at XMWp)"]
        )


    def calc_grid_allowable(self):
        """
        Calculates the maximum amount of energy that can be injected into the grid in kWh,
        Calculates absolute total and also total amount of solar that can be injected at once.
        """
        self.generation["Grid Allowable (RTC)"] = self.customer_RTC * 1000
        self.generation["Grid Allowable (Solar)"] = self.solar_capacity * 1000


    def calc_grid_injected(self):
        """
        Calculates how much wind and solar are to be injected into the grid
        """

        gen = self.generation
        wind_gen = gen["Wind (at XMWp)"]
        solar_gen = gen["Solar (at XMWp)"]
        total_grid = gen["Grid Allowable (RTC)"]
        max_solar = gen["Grid Allowable (Solar)"]
        hour = gen["HRS"]

        # All of the energy produced from wind is injected into the grid immediately, as there is no BESS for wind
        wind_injected = np.where(wind_gen > 0, wind_gen, 0)

        # Checks whether we are in the solar discharging window
        in_solar_window = (hour >= self.solar_discharge_start) & (hour <= self.solar_discharge_end)

        # If we are in the solar discharging window, inject grid with the max solar possible
        solar_injected = np.where(
            # Solar injected is the amount of solar generated, but if solar + wind becomes more than the grid
            # allowable, then we must cap the amount of solar we inject and some will have to be saved
            in_solar_window,
            np.minimum(np.minimum(solar_gen, max_solar), (total_grid - wind_injected)), 0,
        )

        # Save results
        gen["Grid Injected (Wind)"] = wind_injected
        gen["Grid Injected (Solar)"] = solar_injected
        gen["Grid Injected (Total)"] = wind_injected + solar_injected


    def calc_grid_deficit_excess(self):
        gen = self.generation
        max_solar = gen["Grid Allowable (Solar)"]
        injected_solar = gen["Grid Injected (Solar)"]
        solar_gen = gen["Solar (at XMWp)"]
        total_gen = gen["Total Generation"]
        total_injected = gen["Grid Injected (Total)"]

        # Calculates the deficit in the amount of solar that we injected compared to the maximum we could inject
        gen["Grid Deficit (Solar)"] = max_solar - injected_solar

        # Calculates how much solar we generated but did not inject
        gen["Grid Excess (Solar)"] = total_gen - total_injected

    def calc_soc_and_bess(self):
        gen = self.generation
        n_rows = len(gen)

        hours = gen["HRS"].to_numpy()
        solar_deficit = gen["Grid Deficit (Solar)"].to_numpy()
        solar_excess = gen["Grid Excess (Solar)"].to_numpy()

        max_SoC = self.max_SoC * 1000
        min_SoC = self.min_SoC * 1000

        BESS_SoC = np.zeros(n_rows)
        Injected_BESS = np.zeros(n_rows)
        Solar_Curtailed = np.zeros(n_rows)

        # We start the battery at its minimum state of charge, and in this first step no power is injected from the battery
        BESS_SoC[0] = min_SoC
        Injected_BESS[0] = 0
        Solar_Curtailed[0] = 0

        for t in range(1, n_rows):
            
            prev_SoC = BESS_SoC[t - 1]

            # Check if we are in the window where we discharge BESS
            in_bess_window = (self.bess_discharge_start <= hours[t] <= self.bess_discharge_end)

            # These calculate how much capacity is left in the battery to charge and discharge.
            charge_room = max_SoC - prev_SoC
            discharge_room = prev_SoC - min_SoC

            # We inject however much we can to cover the solar_deficit, but only if we are in the BESS window
            bess_discharge = min(solar_deficit[t], discharge_room) if in_bess_window else 0
            bess_discharge = max(0, bess_discharge) if in_bess_window else 0

            # The New BESS State of Charge = Previous SoC + Charged Capacity - Discharge Capacity
            BESS_SoC[t] = min(max_SoC, max(min_SoC, prev_SoC + min(solar_excess[t], charge_room) - (
                min(solar_deficit[t], discharge_room) if in_bess_window else 0
            )))

            # Injected BESS is the same as BESS discharged
            Injected_BESS[t] = max(0, min(solar_deficit[t], discharge_room)) if in_bess_window else 0

            # Amount of Solar that has been wasted
            Solar_Curtailed[t] = max(0, solar_excess[t] - min(solar_excess[t], charge_room))

        # Save Results
        gen["State of Charge_BESS"] = BESS_SoC
        gen["GridInjected_BESS"] = Injected_BESS
        gen["SolarCurtailed_BESS"] = Solar_Curtailed


    def calc_dc_coupled_total(self):

        # Calculates the total amount of energy injected into the grid for the full Wind-Solar+BESS system
        gen = self.generation
        gen["GridInjected_Solar+Wind+BESS"] = (
            gen["GridInjected_BESS"] + gen["Grid Injected (Total)"]
        )

    def run(self):
        self.calc_total_gen()
        self.calc_grid_allowable()
        self.calc_grid_injected()
        self.calc_grid_deficit_excess()
        self.calc_soc_and_bess()
        self.calc_dc_coupled_total()
        return self.generation

    def calc_settlement_table(self):
    
            gen = self.generation

            # Calculates the settlement table, summing up the amount of power injected into the 
            # grid over each hour of each month
            pivot = gen.pivot_table(
                index="HRS",
                columns="Month",
                values="GridInjected_Solar+Wind+BESS",
                aggfunc="sum",
                fill_value=0,
            )
            # Guarantee all 24 hours x 12 months are present, in the sheet's order
            pivot = pivot.reindex(index=range(1, 25), columns=range(1, 13), fill_value=0)

            # Calculates total
            pivot["Total"] = pivot.sum(axis=1)

            total_row = pivot.sum(axis=0)
            total_row.name = "Total"
    
            settlement = pd.concat([pivot, total_row.to_frame().T])
            settlement.index.name = "HRS"
    
            self.settlement_table = settlement
            return settlement

    def calc_generation_table(self):
            """
            This will calculate the total energy generation table, 
            grouping generation into the Normal, Solar and Peak hours
            """
    
            if not hasattr(self, "settlement_table"):
                self.calc_settlement_table()
    
            # HRS x Month grid only, this will drop the "Totals" column and row
            monthly = self.settlement_table.loc[1:24, 1:12]


            total_discharged = self.generation["GridInjected_Solar+Wind+BESS"].sum()
            total_bess_discharged = self.generation["GridInjected_BESS"].sum()

            # For every bit of power discharged through the BESS, we need to account for Round-Trip Efficiency.
            rte_adjusted_total = total_discharged - total_bess_discharged * (1 - self.RTE)
            rte_factor = rte_adjusted_total / total_discharged
    
            month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

            # Splits into the specific time of day blocks, for normal, solar and peak hours
            tod_blocks = {
                "Normal": range(1, 10),   # HRS 1-9 
                "Solar":  range(10, 18),  # HRS 10-17 
                "Peak":   range(18, 25),  # HRS 18-24
            }

            # Sums the hours for the normal, solar and peak ranges
            rows = {
                label: monthly.loc[list(hrs_range)].sum(axis=0) * rte_factor
                for label, hrs_range in tod_blocks.items()
            }

            # Puts the rows into a dataframe
            gen_table = pd.DataFrame(rows).T
            gen_table.columns = month_labels
            gen_table["Total"] = gen_table.sum(axis=1)
    
            total_row = gen_table.sum(axis=0)
            total_row.name = "Total"
            gen_table = pd.concat([gen_table, total_row.to_frame().T])
    
            gen_table["Average"] = gen_table["Total"] / gen_table.loc["Total", "Total"]
    
            self.generation_table = gen_table
            return gen_table

    def calc_consumption_table(self, normal_share=0.3, solar_share=0.4, peak_share=0.3):
        """
        This calculates an estimate for customer consumption 
        using the customer load factor provided as a parameter
        """
        annual_total = self.customer_RTC * 1000 * 365 * 24 * self.customer_load_factor

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        shares = {"Normal": normal_share, "Solar": solar_share, "Peak": peak_share}

        rows = {}

        # Estimates consumption based on the relative parameters of normal share, solar share and peak share
        for label, share in shares.items():
            block_total = share * annual_total
            rows[label] = pd.Series(
                [block_total / 12] * 12 + [block_total, share],
                index=month_labels + ["Total", "Average"],
            )

        # Converts to a dataframe and calculates totals for each month and each time of day block
        table = pd.DataFrame(rows).T

        total_row = table[month_labels + ["Total"]].sum(axis=0)
        total_row["Average"] = table["Average"].sum()  # should be 1.0
        total_row.name = "Total"

        table = pd.concat([table, total_row.to_frame().T])

        self.consumption_table = table
        return table

    def calc_replacement(self, consumption_table=None):

        if not hasattr(self, "generation_table"):
            self.calc_generation_table()

        if consumption_table is None:
            if not hasattr(self, "consumption_table"):
                self.calc_consumption_table()
            
            consumption_table = self.consumption_table

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        gen = self.generation_table
        cols = month_labels + ["Total"]

        # Calculates the percentage of the customer consumption the whole plant can replace 
        replacement = gen[cols] / consumption_table[cols]

        # Calculates the average across the 3 time of day blocks
        replacement["Average"] = gen["Total"] / consumption_table["Total"]

        replacement_table = replacement.drop(columns="Total")

        self.replacement_table = replacement_table
        replacement = replacement_table.at["Total", "Average"]
        return replacement_table, replacement

    def calc_energy_settlement(self, consumption_table=None):
        """
        Settles any energy generation including banking and solar curtailment
        """
        if not hasattr(self, "generation_table"):
            self.calc_generation_table()

        if consumption_table is None:
            if not hasattr(self, "consumption_table"):
                self.calc_consumption_table()
            consumption_table = self.consumption_table

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        gen = self.generation_table
        cons = consumption_table

        gen_normal = gen.loc["Normal", month_labels]
        gen_solar = gen.loc["Solar", month_labels]
        gen_peak = gen.loc["Peak", month_labels]

        cons_normal = cons.loc["Normal", month_labels]
        cons_solar = cons.loc["Solar", month_labels]
        cons_peak = cons.loc["Peak", month_labels]

    
        peak_surplus = (gen_peak - cons_peak).clip(lower=0)
        solar_deficit = (cons_solar - gen_solar).clip(lower=0)

        # Any extra generation that we didn't use in peak hours can be banked into solar hours first
        banked_to_solar = np.minimum(peak_surplus, solar_deficit)

        # If after banking to solar hours, there is still a peak surplus we bank them into normal hours
        banked_to_normal = (peak_surplus - banked_to_solar).clip(lower=0)


        normal_surplus = (gen_normal - cons_normal).clip(lower=0)

        # Settles each of time of day blocks individually
        peak_settled = np.minimum(gen_peak, cons_peak)
        solar_settled = np.minimum(gen_solar + normal_surplus + banked_to_solar, cons_solar)
        normal_settled = np.minimum(gen_normal + banked_to_normal, cons_normal)

        settlement = pd.DataFrame({
            "Normal": normal_settled,
            "Solar": solar_settled,
            "Peak": peak_settled,
        }).T  # rows = blocks, columns = months

        # Saves results
        settlement["Total"] = settlement.sum(axis=1)

        total_row = settlement.sum(axis=0)
        total_row.name = "Total"
        settlement = pd.concat([settlement, total_row.to_frame().T])

        # Calculates average
        settlement["Average"] = settlement["Total"] / settlement.loc["Total", "Total"]

        self.energy_settlement_table = settlement
        return settlement


    def calc_effective_replacement(self, consumption_table=None):

        if not hasattr(self, "energy_settlement_table"):
            self.calc_energy_settlement(consumption_table)

        if consumption_table is None:
            if not hasattr(self, "consumption_table"):
                self.calc_consumption_table()
            consumption_table = self.consumption_table

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        settlement = self.energy_settlement_table
        cols = month_labels + ["Total"]

        # Calculates the percentage of the customers consumption that we can effectively replace after energy settlements
        effective_replacement = settlement[cols] / consumption_table[cols]

        effective_replacement["Average"] = (settlement["Total"] / consumption_table["Total"])


        effective_replacement_table = effective_replacement.drop(columns="Total")

        self.effective_replacement_table = effective_replacement_table
        effective_replacement_ratio = effective_replacement_table.at["Total", "Average"]
        return effective_replacement_table, effective_replacement_ratio

    def calc_total_discharged(self, consumption_table=None):

        if consumption_table is None:
            if not hasattr(self, "consumption_table"):
                self.calc_consumption_table()
            consumption_table = self.consumption_table

        return self.energy_settlement_table.loc["Total","Total"]
    
    def run_analytics(self, consumption_table=None):

        self.run()

        if consumption_table is None:
            consumption_table = self.calc_consumption_table()

        settlement = self.calc_settlement_table()
        generation_table = self.calc_generation_table()
        replacement = self.calc_replacement(consumption_table)
        energy_settlement = self.calc_energy_settlement(consumption_table)
        effective_replacement_table, effective_replacement = self.calc_effective_replacement(consumption_table)
        discharged = self.calc_total_discharged(consumption_table)

        return effective_replacement, discharged




if __name__ == "__main__":

    # Loads in generation data from the spreadsheet
    data = pd.read_excel(
        "Spreadsheets/Master Spreadsheet.xlsx",
        sheet_name="Wind-Solar+BESS",
        header=4,        
        nrows=8760,     
    )

    # Defines all the parameters
    plant = Wind_SolarBESS(
        generation=data[["Month", "Day", "HRS", "Wind (at XMWp)", "Solar (at XMWp)"]],
        customer_load_factor=0.45,
        solar_capacity=150,
        wind_capacity=49.5,
        BESS_hours=6,
        max_SoC_perc=1,
        min_SoC_perc=0,
        solar_discharge_start=1,
        solar_discharge_end=14,
        bess_discharge_start=18,
        bess_discharge_end=24,
        RTE = 0.85
    )


    print(plant.run_analytics())
    


    