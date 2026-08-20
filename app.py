import streamlit as st
import pandas as pd
import numpy as np
import numpy_financial as npf
import os

# Import the class from your secondary file
# Ensure WindSolarBESS.py is in the same directory!
from WindSolarBESS import Wind_SolarBESS

YEARS = range(26)  # 0 = CAPEX outlay year, 1..25 = operating years

# --- INTEGRATED DASHBOARD CLASS ---
# (Your provided class is included here for seamless execution)
class DashboardStat:
    def __init__(self, gencons, solar_capacity, wind_capacity, BESS_hours, max_SoC_perc, min_SoC_perc,
                 solar_gen_degrad, wind_gen_degrad, BESS_capacity_degrad, RTE_degrad,
                 solar_maintenance, wind_maintenance, BESS_maintenance, costs_escalation,
                 tariff, solar_capex, wind_capex, BESS_capex, RTE=0.85, customer_load_factor=0.45):

        self.gencons = gencons
        self.solar_capacity = solar_capacity
        self.wind_capacity = wind_capacity
        self.customer_RTC = self.solar_capacity + self.wind_capacity
        self.BESS_hours = BESS_hours
        self.max_SoC_perc = max_SoC_perc / 100
        self.min_SoC_perc = min_SoC_perc / 100
        self.RTE = RTE / 100
        self.battery_capacity = self.BESS_hours * self.solar_capacity
        self.solar_capacity_dc = self.solar_capacity * 2
        self.max_SoC = self.max_SoC_perc * self.battery_capacity
        self.min_SoC = self.min_SoC_perc * self.battery_capacity
        self.solar_degrad = solar_gen_degrad
        self.wind_gen_degrad = wind_gen_degrad
        self.BESS_capacity_degrad = BESS_capacity_degrad
        self.RTE_degrad = RTE_degrad
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

    def gencons_to_df(self):
        generation_data = pd.read_excel(self.gencons, sheet_name="Generation", header=3, nrows=8760)
        generation = generation_data[["Month", "Day", "HRS", "Wind (at XMWp)", "Solar (at XMWp)"]]
        
        consumption = pd.read_excel(self.gencons, sheet_name="Consumption", header=0, index_col="Time Block")
        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        consumption = consumption.loc[["Normal", "Solar", "Peak"], month_labels]
        consumption["Total"] = consumption.sum(axis=1)

        total_row = consumption.sum(axis=0)
        total_row.name = "Total"
        consumption = pd.concat([consumption, total_row.to_frame().T])
        consumption["Average"] = consumption["Total"] / consumption.loc["Total", "Total"]

        self.generation = generation
        self.consumption = consumption
        return self

    def _build_plant_for_year(self, year):
        BESS_hours = self.BESS_hours * (((100 - self.BESS_capacity_degrad) / 100) ** (year-1))
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
            generation=gen, customer_load_factor=self.customer_load_factor,
            solar_capacity=self.solar_capacity, wind_capacity=self.wind_capacity,
            BESS_hours=BESS_hours, max_SoC_perc=self.max_SoC_perc, min_SoC_perc=self.min_SoC_perc,
            solar_discharge_start=1, solar_discharge_end=solar_discharge_end,
            bess_discharge_start=18, bess_discharge_end=24, RTE=RTE,
        )
        return plant

    def calc_revenue(self, year):
        if year == 0:
            return 0, 0
        plant = self._build_plant_for_year(year)
        effective_replacement, discharged_kwh = plant.run_analytics(consumption_table=self.consumption)
        discharged = discharged_kwh / 1_000_000.0  
        revenue = discharged * self.tariff
        return revenue, discharged

    def calc_maintenance_cost(self, year):
        if year == 0:
            costs = ((self.solar_capex * 2 * (self.solar_capacity / 1000) + 
                      self.wind_capex * (self.wind_capacity / 1000) + 
                      self.BESS_capex * (self.battery_capacity / 1000)) * 1000) 
            return costs
        else:
            costs = (self.BESS_maintenance * self.battery_capacity + 
                     self.solar_maintenance * self.solar_capacity_dc + 
                     self.wind_maintenance * self.wind_capacity) * 1.18 / 1000000 + 0.5
            costs = costs * (((100 + self.costs_escalation) / 100) ** (year-1))
            return costs

    def calc_irr_table(self):
        rows = []
        for i in YEARS:
            revenue, discharged = self.calc_revenue(i)
            costs = self.calc_maintenance_cost(i)
            rows.append({
                "Year": i, "Discharged_MnkWh": discharged,
                "Revenue_RsCr": revenue, "Costs_RsCr": costs,
                "EBITDA_RsCr": revenue - costs,
            })
        self.irr_table = pd.DataFrame(rows)
        return self.irr_table

    def calc_irr(self):
        if self.irr_table is None:
            self.calc_irr_table()
        return npf.irr(self.irr_table["EBITDA_RsCr"])

    def calc_capex_ebitda_ratio(self):
        if self.irr_table is None:
            self.calc_irr_table()
        capex = self.irr_table.loc[self.irr_table["Year"] == 0, "Costs_RsCr"].iloc[0]
        avg_ebitda = self.irr_table.loc[self.irr_table["Year"] >= 1, "EBITDA_RsCr"].mean()
        return capex / avg_ebitda

    def save_irr_table(self, path: str = "Revenue_Costs_EBITDA_Table.xlsx"):
        if self.irr_table is None:
            self.calc_irr_table()
        self.irr_table.to_excel(path, sheet_name="Revenue_Costs_EBITDA", index=False)
        return path

    def calc_effective_replacement(self, year=1):
        plant = self._build_plant_for_year(year)
        effective_replacement, _ = plant.run_analytics(consumption_table=self.consumption)
        return effective_replacement

    def calc_effective_replacement_no_bess(self, year=1):
        RTE = self.RTE * (((100 - self.RTE_degrad) / 100) ** (year-1))
        gen = self.generation.copy()
        gen["Wind (at XMWp)"] = self.generation["Wind (at XMWp)"] * (((100 - self.wind_gen_degrad) / 100) ** year)
        gen["Solar (at XMWp)"] = self.generation["Solar (at XMWp)"] * (((100 - self.solar_degrad) / 100) ** year)

        plant_no_bess = Wind_SolarBESS(
            generation=gen, customer_load_factor=self.customer_load_factor,
            solar_capacity=self.solar_capacity, wind_capacity=self.wind_capacity,
            BESS_hours=0, max_SoC_perc=self.max_SoC_perc, min_SoC_perc=self.min_SoC_perc,
            solar_discharge_start=1, solar_discharge_end=14,
            bess_discharge_start=18, bess_discharge_end=24, RTE=RTE,
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

    def run_dashboard(self, irr_table_path: str = "Revenue_Costs_EBITDA_Table.xlsx") -> dict:
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


# --- STREAMLIT UI ---
st.set_page_config(page_title="Wind-Solar and BESS Simulation", layout="wide")
col1, col2, col3 = st.columns([2, 2, 1])

with col3:
    st.image("company_logo.png", width=200)

st.title("Wind-Solar BESS Simulation")
st.markdown("Upload your generation and consumption, fill in the parameters for the plant, and run the simulation!")

# -- 1. File Handling: Download Template & Upload Filled File
st.header("1. Upload Generation and Consumption")
col_down, col_up = st.columns(2)

with col_down:
    st.markdown("Download the empty Excel template, fill in your generation and consumption data, and upload it back.")
    try:
        with open("Template.xlsx", "rb") as template_file:
            st.download_button(
                label="📥 Download Template.xlsx",
                data=template_file,
                file_name="Template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
    except FileNotFoundError:
        st.warning("⚠️ 'Template.xlsx' not found in the local directory. Please ensure it is present for users to download.")

with col_up:
    uploaded_file = st.file_uploader("📤 Upload your filled-in Template file", type=["xlsx"])

st.divider()

# -- 2. Parameter Inputs (Grouped on Main Screen)
st.header("2. Parameters")

st.subheader("State of Charge")
col1, col2, col3 = st.columns(3)
max_soc = col1.number_input("Max SoC (%)", min_value=0, max_value=100, value=100, step=5)
min_soc = col2.number_input("Min SoC (%)", min_value=0, max_value=100, value=0, step=5)
rte_year1 = col3.number_input("RTE Year 1 (%)", min_value=0, max_value=100, value=85, step=5)

st.subheader("Generation and Storage Capacity")
col4, col5, col6 = st.columns(3)
solar_cap = col4.number_input("Solar Capacity (MW)", value=150.0, step=10.0)
wind_cap = col5.number_input("Wind Capacity (MW)", value=49.5, step=5.0)
bess_hours = col6.number_input("BESS Hours", value=6.0, step=1.0)

st.subheader("Degradation (%)")
col7, col8, col9, col10 = st.columns(4)
solar_deg = col7.number_input("Solar Degradation (%/yr)", value=0.5, step=0.1)
wind_deg = col8.number_input("Wind Degradation (%/yr)", value=0.2, step=0.1)
bess_cap_deg = col9.number_input("BESS Capacity Degradation (%/yr)", value=2.0, step=0.1)
bess_rte_deg = col10.number_input("BESS RTE Degradation (%/yr)", value=0.1, step=0.1)

st.subheader("CapEx (INR Mn / Capacity)")
col11, col12, col13 = st.columns(3)
solar_capex = col11.number_input("Solar CAPEX", value=40, step=1)
wind_capex = col12.number_input("Wind CAPEX", value=90, step=1)
bess_capex = col13.number_input("BESS CAPEX", value=10, step=1)

st.subheader("Maintenance Cost (Lakh Rs / MW / Year)")
col14, col15, col16, col17 = st.columns(4)
solar_maint = col14.number_input("Solar Maintenance (Lakh Rs)", value=5.0, step=0.5)
wind_maint = col15.number_input("Wind Maintenance (Lakh Rs)", value=9.1, step=0.5)
bess_maint = col16.number_input("BESS Maintenance (Lakh Rs)", value=1.0, step=0.5)
cost_esc = col17.number_input("Cost Escalation (%/yr)", value=3.0, step=0.5)

st.subheader("Tariff")
tariff_input = st.number_input("Tariff (Rs/kWh)", value=6.0, step=0.5)

st.divider()

# -- 3. Execution & Results Display
st.header("3. Run the Simulation!")

if st.button("Run Simulation", type="primary"):
    if uploaded_file is None:
        st.error("Please upload the filled-in template file before running")
    else:
        with st.spinner("Running 25-year simulation..."):
            # Pandas needs a file path to process multiple sheets cleanly in the logic
            temp_file_path = "temp_uploaded_gencons.xlsx"
            with open(temp_file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
                
            try:
                # Initialize the dashboard class with UI inputs
                dash = DashboardStat(
                    gencons=temp_file_path,
                    solar_capacity=solar_cap,
                    wind_capacity=wind_cap,
                    BESS_hours=bess_hours,
                    max_SoC_perc=max_soc,
                    min_SoC_perc=min_soc,
                    solar_gen_degrad=solar_deg,
                    wind_gen_degrad=wind_deg,
                    BESS_capacity_degrad=bess_cap_deg,
                    RTE_degrad=bess_rte_deg,
                    solar_maintenance=solar_maint * 100000,
                    wind_maintenance=wind_maint * 100000,
                    BESS_maintenance=bess_maint * 100000,
                    costs_escalation=cost_esc,
                    tariff=tariff_input,
                    solar_capex=solar_capex,
                    wind_capex=wind_capex,
                    BESS_capex=bess_capex,
                    RTE=rte_year1
                )
                
                output_path = "Revenue_Costs_EBITDA_Table.xlsx"
                results = dash.run_dashboard(irr_table_path=output_path)
                
                st.success("Optimisation successfully completed!")
                
                # Display Metrics
                st.subheader("Key Performance Indicators")
                metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
                
                # Displaying formatted numbers
                irr_val = results["irr"]
                metric_col1.metric("Project IRR", f"{irr_val:.2%}" if not pd.isna(irr_val) else "N/A")
                metric_col2.metric("Capex/EBITDA Ratio", f"{results['capex_to_ebitda_ratio']:.2f}")
                metric_col3.metric("Effective Replacement with BESS", f"{(results['Effective_Replacement_With_BESS']*100):.2f}%")
                metric_col4.metric("Effective Replacement without BESS", f"{(results['Effective_Replacement_Without_BESS']*100):.2f}%")
                
                # Output File Download
                st.markdown("### Download Simulation Results")
                with open(output_path, "rb") as out_file:
                    st.download_button(
                        label="📊 Download Revenue, Costs & EBITDA Table",
                        data=out_file,
                        file_name="Revenue_Costs_EBITDA_Table.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            
            except Exception as e:
                st.error(f"An error occurred during simulation: {e}")
            
            finally:
                # Clean up temporary uploaded file
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
