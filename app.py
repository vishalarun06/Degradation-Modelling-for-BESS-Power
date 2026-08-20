import streamlit as st
import pandas as pd
import numpy as np
import numpy_financial as npf
import os
import plotly.express as px

# Import the class from your secondary file
# Ensure WindSolarBESS.py is in the same directory!
from WindSolarBESS import Wind_SolarBESS

YEARS = range(26)  # 0 = CAPEX outlay year, 1..25 = operating years

# --- INTEGRATED DASHBOARD CLASS ---
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

st.markdown("""
<style>
div[data-testid="metric-container"] {
    background-color: #1E293B;
    border: 1px solid #334155;
    padding: 5% 5% 5% 10%;
    border-radius: 8px;
    border-left: 5px solid #F37021;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}
</style>
""", unsafe_allow_html=True)

# --- SIDEBAR: CONTROLS & PARAMETERS ---
with st.sidebar:
    st.image("company_logo.png", width=180)
    st.markdown("### Simulation Parameters")
    
    st.markdown("**1. Core Capacities**")
    solar_cap = st.number_input("Solar (MW)", value=150.0, step=10.0)
    wind_cap = st.number_input("Wind (MW)", value=49.5, step=5.0)
    bess_hours = st.number_input("BESS Hours", value=6.0, step=1.0)
    
    st.markdown("**2. Financials**")
    tariff_input = st.number_input("Tariff (Rs/kWh)", value=6.0, step=0.5)
    solar_capex = st.number_input("Solar CapEx (Mn)", value=40, step=1)
    wind_capex = st.number_input("Wind CapEx (Mn)", value=90, step=1)
    bess_capex = st.number_input("BESS CapEx (Mn)", value=10, step=1)

    # Advanced parameters grouped in an expander to save space
    with st.expander("⚙️ Advanced Settings (Degradation & Maint.)"):
        st.markdown("**State of Charge & RTE**")
        max_soc = st.number_input("Max SoC (%)", min_value=0, max_value=100, value=100, step=5)
        min_soc = st.number_input("Min SoC (%)", min_value=0, max_value=100, value=0, step=5)
        rte_year1 = st.number_input("RTE Year 1 (%)", min_value=0, max_value=100, value=85, step=5)
        
        st.markdown("**Degradation (%/yr)**")
        solar_deg = st.number_input("Solar", value=0.5, step=0.1)
        wind_deg = st.number_input("Wind", value=0.2, step=0.1)
        bess_cap_deg = st.number_input("BESS Capacity", value=2.0, step=0.1)
        bess_rte_deg = st.number_input("BESS RTE", value=0.1, step=0.1)
        
        st.markdown("**Maintenance (Lakh) & Escalation**")
        solar_maint = st.number_input("Solar Maint", value=5.0, step=0.5)
        wind_maint = st.number_input("Wind Maint", value=9.1, step=0.5)
        bess_maint = st.number_input("BESS Maint", value=1.0, step=0.5)
        cost_esc = st.number_input("Cost Escalation (%)", value=3.0, step=0.5)

# --- MAIN CANVAS: DATA UPLOAD & RESULTS ---
st.title("Wind Solar BESS Optimization")
st.markdown("<p style='color: #9CA3AF;'>Configure parameters in the sidebar, upload your template, and execute the 25-year financial simulation.</p>", unsafe_allow_html=True)

# 1. File Handling Container
with st.container(border=True):
    col_down, col_up = st.columns(2)
    with col_down:
        st.markdown("**1. Download Template**")
        try:
            with open("Template.xlsx", "rb") as template_file:
                st.download_button("Download Template File", data=template_file, file_name="Template.xlsx", use_container_width=True)
        except FileNotFoundError:
            st.warning("Template.xlsx missing.")

    with col_up:
        st.markdown("**2. Upload Data**")
        uploaded_file = st.file_uploader("Upload filled template", type=["xlsx"], label_visibility="collapsed")

st.write("") # Spacer

# 2. Execution button
if st.button("Run Financial Simulation", type="primary", use_container_width=True):
    if uploaded_file is None:
        st.error("Please upload the filled-in template file before running.")
    else:
        with st.spinner("Simulating 25-year generation and cash flows..."):
            temp_file_path = "temp_uploaded_gencons.xlsx"
            with open(temp_file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
                
            try:
                # Initialize DashboardStat (Assume dash is initialized exactly as you had it)
                dash = DashboardStat(
                    gencons=temp_file_path, solar_capacity=solar_cap, wind_capacity=wind_cap, BESS_hours=bess_hours,
                    max_SoC_perc=max_soc, min_SoC_perc=min_soc, solar_gen_degrad=solar_deg, wind_gen_degrad=wind_deg,
                    BESS_capacity_degrad=bess_cap_deg, RTE_degrad=bess_rte_deg, solar_maintenance=solar_maint * 100000,
                    wind_maintenance=wind_maint * 100000, BESS_maintenance=bess_maint * 100000, costs_escalation=cost_esc,
                    tariff=tariff_input, solar_capex=solar_capex, wind_capex=wind_capex, BESS_capex=bess_capex, RTE=rte_year1
                )
                
                output_path = "Revenue_Costs_EBITDA_Table.xlsx"
                results = dash.run_dashboard(irr_table_path=output_path)
                
                # --- RESULTS DASHBOARD ---
                st.divider()
                st.subheader("Financial Overview")
                
                # Top Row Metrics (Styled by our custom CSS above)
                metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
                irr_val = results["irr"]
                metric_col1.metric("Project IRR", f"{irr_val:.2%}" if not pd.isna(irr_val) else "N/A")
                metric_col2.metric("CapEx/EBITDA Ratio", f"{results['capex_to_ebitda_ratio']:.2f}")
                metric_col3.metric("Effective Repl. (w/ BESS)", f"{(results['Effective_Replacement_With_BESS']*100):.2f}%")
                metric_col4.metric("Effective Repl. (w/o BESS)", f"{(results['Effective_Replacement_Without_BESS']*100):.2f}%")
                
                st.write("") # Spacer
                
                # Professional Chart using Plotly
                st.markdown("**25-Year Cash Flow Projection (EBITDA)**")
                df_chart = results["irr_table"][results["irr_table"]["Year"] > 0] # Filter out Year 0 (CapEx)
                fig = px.bar(df_chart, x="Year", y=["Revenue_RsCr", "Costs_RsCr", "EBITDA_RsCr"], 
                             barmode='group',
                             color_discrete_sequence=["#38BDF8", "#EF4444", "#F37021"],
                             labels={"value": "Rs Cr", "variable": "Metric"})
                fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", margin=dict(l=0, r=0, t=30, b=0))
                st.plotly_chart(fig, use_container_width=True)
                
                # Output File Download
                with open(output_path, "rb") as out_file:
                    st.download_button("Download Full Financial Model (Excel)", data=out_file, file_name="Revenue_Costs_EBITDA_Table.xlsx", use_container_width=True)
            
            except Exception as e:
                st.error(f"An error occurred during simulation: {e}")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
