from gym import spaces
import numpy as np
import random
import os
import string
import json
import pandas as pd
import pandapower as pp
from pandapower import runpp
import gym
from collections import OrderedDict
def subhourly_lin_interp(hourly_data, subhourly_steps):
    """ Returns a linear interpolation of a data array as a list """
    n = len(hourly_data)
    data = np.interp(np.linspace(0, n, n*subhourly_steps), np.arange(n), hourly_data)
    return list(data)

def subhourly_noisy_interp(hourly_data, subhourly_steps):
    """ Returns a noisy distribution of power consumption +/- 5% standard deviation of the original power draw."""
    n = len(hourly_data)
    data = np.repeat(hourly_data, subhourly_steps)
    perturbation = np.random.normal(1.0, 0.05, n*subhourly_steps)
    data = np.multiply(data, perturbation)
    return list(data)

def subhourly_randomdraw_interp(hourly_data, subhourly_steps, dhw_pwr):
    """ Returns a randomized binary distribution where demand = power*time when water is drawn, 0 otherwise.
    Proportion of time with demand at full power corresponds to energy consumption at the hourly interval by E+ """
    data = []
    subhourly_dhw_energy = max(0.01, dhw_pwr / subhourly_steps)
    for hour in hourly_data:
        draw_times = np.random.choice(subhourly_steps, int(hour/subhourly_dhw_energy), replace=False)
        for i in range(subhourly_steps):
            if i in draw_times:
                data += [subhourly_dhw_energy]
            else:
                data += [0]
    return list(data)

class Building:
    def __init__(self, data_path, climate_zone, buildings_states_actions_file, hourly_timesteps, uid, save_memory = True):
        """
        Args:
            buildingId (int)
            dhw_storage (EnergyStorage)
            cooling_storage (EnergyStorage)
            electrical_storage (Battery)
            dhw_heating_device (ElectricHeater or HeatPump)
            cooling_device (HeatPump)
        """
        weather_file = os.path.join(data_path, "weather_data.csv")
        solar_file = os.path.join(data_path, "solar_generation_1kW.csv")
        self.hourly_timesteps = hourly_timesteps

        # create a Unique Building ID
        self.buildingId = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))

        with open(buildings_states_actions_file) as json_file:
            buildings_states_actions = json.load(json_file, object_pairs_hook=OrderedDict)
        # print('keys', buildings_states_actions.keys())

        self.uid = uid

        # create all the systems that go in the house
        attributes_file = os.path.join(data_path, "building_attributes.json")
        attributes = self.set_attributes(attributes_file)
        save_memory = True

        # not sure i need these...
        self.save_memory = save_memory
        self.create_systems(attributes)

        # get observation and action spaces for the RL agent
        tmp = buildings_states_actions[self.uid]
        self.enabled_states = tmp['states']
        self.enabled_actions = tmp['actions']

        # get e-plus load calcs
        sim_file = os.path.join(data_path, f"Building_{self.building_type}.csv")
        self.sim_results = self.load_sim_results(sim_file)

        weather_res = self.load_weather_results(weather_file)
        self.sim_results.update(weather_res)

        solar_res = self.load_solar_results(solar_file, attributes)
        self.sim_results.update(solar_res)

        self.set_dhw_cop()
        self.set_cooling_cop()
        self.autosize_equipment()
        self.set_dhw_draws()

        self.set_state_space()
        self.set_action_space()
        # reset/initialize the home to timestep = 0
        # self.grid = self.add_grid(grid)
        self.time_step = 0
        self.current_gross_electricity_demand = 0
        self.current_gross_generation = 0
        self.phi = 0
        self.rbc = False
        # self.reset()
        self.battery_action = 0
        self.action_log = []
        self.pv_log = []
        self.unshiftload_log = []
        self.dhwload_log = []
        self.hvacload_log = []

        self.night, self.morning = np.random.randint(20,22), np.random.randint(4,10)

    def assign_bus(self, bus):
        self.bus = bus

    def set_attributes(self, file):
        with open(file) as json_file:
            data = json.load(json_file, object_pairs_hook=OrderedDict)
        # print('this',data.keys(), self.uid)
        tmp = data[self.uid]
        self.building_type = tmp['Building_Type']
        # print(self.building_type, self.uid)
        self.climate_zone = tmp['Climate_Zone']
        return data[self.uid]

    def create_systems(self, attributes):
        # create all the subcomponents of the building
        self.cooling_device = HeatPump(nominal_power = attributes['Heat_Pump']['nominal_power'],
                             eta_tech = attributes['Heat_Pump']['technical_efficiency'],
                             t_target_heating = attributes['Heat_Pump']['t_target_heating'],
                             t_target_cooling = attributes['Heat_Pump']['t_target_cooling'], save_memory = self.save_memory)

        self.dhw_heating_device = ElectricHeater(nominal_power = attributes['Electric_Water_Heater']['nominal_power'],
                                         efficiency = attributes['Electric_Water_Heater']['efficiency'], save_memory = self.save_memory)

        self.cooling_storage = EnergyStorage(hourly_steps = self.hourly_timesteps, capacity = attributes['Chilled_Water_Tank']['capacity'],
                                           loss_coeff = attributes['Chilled_Water_Tank']['loss_coefficient'], save_memory = self.save_memory)

        self.dhw_storage = EnergyStorage(hourly_steps = self.hourly_timesteps,capacity = attributes['DHW_Tank']['capacity'],
                                 loss_coeff = attributes['DHW_Tank']['loss_coefficient'], save_memory = self.save_memory)

        self.electrical_storage = Battery(hourly_timesteps= self.hourly_timesteps, capacity = attributes['Battery']['capacity'],
                                         capacity_loss_coeff = attributes['Battery']['capacity_loss_coefficient'],
                                         loss_coeff = attributes['Battery']['loss_coefficient'],
                                         efficiency = attributes['Battery']['efficiency'],
                                         nominal_power = attributes['Battery']['nominal_power'],
                                         power_efficiency_curve = attributes['Battery']['power_efficiency_curve'],
                                         capacity_power_curve = attributes['Battery']['capacity_power_curve'],
                                         save_memory = self.save_memory)

        self.solar_power_capacity = attributes['Solar_Power_Installed(kW)']
        return

    def load_sim_results(self, sim_file):
        with open(sim_file) as csv_file:
            data = pd.read_csv(csv_file)

        res = {}
        res['cooling_demand'] = subhourly_lin_interp(data['Cooling Load [kWh]'], self.hourly_timesteps)
        res['dhw_demand'] = list(data['DHW Heating [kWh]'])
        res['non_shiftable_load'] = subhourly_noisy_interp(data['Equipment Electric Power [kWh]'], self.hourly_timesteps)
        res['month'] = list(np.repeat(data['Month'], self.hourly_timesteps))
        res['day'] = list(np.repeat(data['Day Type'], self.hourly_timesteps))
        res['hour'] = list(np.repeat(data['Hour'], self.hourly_timesteps))
        res['daylight_savings_status'] = list(np.repeat(data['Daylight Savings Status'], self.hourly_timesteps))
        res['t_in'] = subhourly_lin_interp(data['Indoor Temperature [C]'], self.hourly_timesteps)
        res['avg_unmet_setpoint'] = subhourly_lin_interp(data['Average Unmet Cooling Setpoint Difference [C]'], self.hourly_timesteps)
        res['rh_in'] = subhourly_lin_interp(data['Indoor Relative Humidity [%]'], self.hourly_timesteps)
        return res

    def load_weather_results(self, weather_file):
        with open(weather_file) as csv_file:
            weather_data = pd.read_csv(csv_file)

        res = {}
        res['t_out'] = subhourly_lin_interp(weather_data['Outdoor Drybulb Temperature [C]'], self.hourly_timesteps)
        res['rh_out'] = subhourly_lin_interp(weather_data['Outdoor Relative Humidity [%]'], self.hourly_timesteps)
        res['diffuse_solar_rad'] = subhourly_lin_interp(weather_data['Diffuse Solar Radiation [W/m2]'], self.hourly_timesteps)
        res['direct_solar_rad'] = subhourly_lin_interp(weather_data['Direct Solar Radiation [W/m2]'], self.hourly_timesteps)

        res['t_out_pred_6h'] = subhourly_lin_interp(weather_data['6h Prediction Outdoor Drybulb Temperature [C]'], self.hourly_timesteps)
        res['t_out_pred_12h'] = subhourly_lin_interp(weather_data['12h Prediction Outdoor Drybulb Temperature [C]'], self.hourly_timesteps)
        res['t_out_pred_24h'] = subhourly_lin_interp(weather_data['24h Prediction Outdoor Drybulb Temperature [C]'], self.hourly_timesteps)

        res['rh_out_pred_6h'] = subhourly_lin_interp(weather_data['6h Prediction Outdoor Relative Humidity [%]'], self.hourly_timesteps)
        res['rh_out_pred_12h'] = subhourly_lin_interp(weather_data['12h Prediction Outdoor Relative Humidity [%]'], self.hourly_timesteps)
        res['rh_out_pred_24h'] = subhourly_lin_interp(weather_data['24h Prediction Outdoor Relative Humidity [%]'], self.hourly_timesteps)

        res['diffuse_solar_rad_pred_6h'] = subhourly_lin_interp(weather_data['6h Prediction Diffuse Solar Radiation [W/m2]'], self.hourly_timesteps)
        res['diffuse_solar_rad_pred_12h'] = subhourly_lin_interp(weather_data['12h Prediction Diffuse Solar Radiation [W/m2]'], self.hourly_timesteps)
        res['diffuse_solar_rad_pred_24h'] = subhourly_lin_interp(weather_data['24h Prediction Diffuse Solar Radiation [W/m2]'], self.hourly_timesteps)

        res['direct_solar_rad_pred_6h'] = subhourly_lin_interp(weather_data['6h Prediction Direct Solar Radiation [W/m2]'], self.hourly_timesteps)
        res['direct_solar_rad_pred_12h'] = subhourly_lin_interp(weather_data['12h Prediction Direct Solar Radiation [W/m2]'], self.hourly_timesteps)
        res['direct_solar_rad_pred_24h'] = subhourly_lin_interp(weather_data['24h Prediction Direct Solar Radiation [W/m2]'], self.hourly_timesteps)
        return res

    def load_solar_results(self, solar_file, attributes):
        with open(solar_file) as csv_file:
            data = pd.read_csv(csv_file)

        res = {}
        res['solar_gen'] = subhourly_lin_interp(attributes['Solar_Power_Installed(kW)']*data['Hourly Data: AC inverter power (W)']/1000, self.hourly_timesteps)
        return res

    def assign_neighbors(self, net):
        my_x = net.bus_geodata.loc[self.bus]['x']
        my_y = net.bus_geodata.loc[self.bus]['y']
        net.bus_geodata['distance'] = (net.bus_geodata['x']-my_x)**2 + (net.bus_geodata['y']-my_y)**2
        self.neighbors = net.bus_geodata.sort_values('distance').drop(index=0).index[1:4]
        return

    def get_reward(self, net): # dummy cost function
        #my_voltage_dev = (10*np.clip(net.res_bus.loc[self.bus]['vm_pu']-1,-.1,.1))**2
        my_voltage_dev = (20*net.res_bus.loc[self.bus]['vm_pu']-1)**2
        my_cons = (self.current_gross_electricity_demand - self.net_elec_cons_mid) / self.net_elec_cons_range
        my_neighbors_voltage_dev = sum(np.square(10 * np.clip(net.res_bus.loc[self.neighbors]['vm_pu']-1,-.1,.1)))
        reward = -1 * (my_voltage_dev + 0.3*my_neighbors_voltage_dev) #+ my_neighbors_voltage_dev)
        #print(reward)
        reward = (reward + 365)/9
        # if not self.rbc:
        #     if self.solar_generation <= 0.000000001:
        #         if self.action_angle:
        #             reward -= 0.05*(self.action_angle - 1)
        #         if self.action_curtail:
        #             reward -= 0.05*(self.action_curtail - 1)
        #print(reward)
        return reward

    def get_obs(self, net):
        s = []
        all_states = []
        for state_name, value in self.enabled_states.items():
            if value == True:
                if state_name == "net_electricity_consumption":
                    all_states += [1]
                    s.append(self.current_gross_electricity_demand)

                elif state_name == "absolute_voltage":
                    all_states += [2]
                    if self.time_step <= 1:
                        s.append(1.0)
                    else:
                        s.append(float(net.res_bus['vm_pu'][net.load.loc[net.load['name']==self.buildingId].bus]))
                elif state_name == "relative_voltage":
                    all_states += [3]
                    if self.time_step <= 1:
                        s.append(0.5)
                    else:
                        ranked_voltage = float(net.res_bus['vm_pu'].rank(pct=True)[net.load.loc[net.load['name']==self.buildingId].bus])
                        s.append(ranked_voltage)

                elif state_name == "total_voltage_spread":
                    all_states += [4]
                    if self.time_step <= 1:
                        s.append(0)
                    else:
                        voltage_spread = 0
                        for index, line in net.line.iterrows():
                            voltage_spread += abs(net.res_bus.loc[line.to_bus].vm_pu - net.res_bus.loc[line.from_bus].vm_pu)
                        s.append(voltage_spread)

                elif state_name != 'cooling_storage_soc' and state_name != 'dhw_storage_soc' and state_name != 'electrical_storage_soc':
                    all_states += [5]
                    s.append(self.sim_results[state_name][self.time_step])

                # elif state_name in ['t_in', 'avg_unmet_setpoint', 'rh_in', 'non_shiftable_load', 'solar_gen']:
                #     all_states += [6]
                #     s.append(self.sim_results[state_name][self.time_step])

                else: # if state_name = cooling_storage_soc,
                    if state_name == 'cooling_storage_soc':
                        all_states += [7]
                        s.append(self.cooling_storage._soc/self.cooling_storage.capacity)
                    elif state_name == 'dhw_storage_soc':
                        all_states += [8]
                        s.append(self.dhw_storage._soc/self.dhw_storage.capacity)
                    elif state_name == 'electrical_storage_soc':
                        all_states += [9]
                        s.append(self.electrical_storage._soc/self.electrical_storage.capacity)
        return (np.array(s) - self.normalization_mid) / self.normalization_range

    def close(self, folderName):
        np.savetxt(f'models/{folderName}/homes/{self.buildingId}_actions.csv', np.array(self.action_log), delimiter=',', fmt='%s')
        # np.savetxt(f'models/{folderName}/homes/{self.buildingId}_pv.csv', np.array(self.pv_log), delimiter=',', fmt='%s')
        # np.savetxt(f'models/{folderName}/homes/{self.buildingId}_hvacload.csv', np.array(self.hvacload_log), delimiter=',', fmt='%s')
        # np.savetxt(f'models/{folderName}/homes/{self.buildingId}_unshiftload.csv', np.array(self.unshiftload_log), delimiter=',', fmt='%s')
        # np.savetxt(f'models/{folderName}/homes/{self.buildingId}_dhwload.csv', np.array(self.dhwload_log), delimiter=',', fmt='%s')
        return

    def step(self, a):
        self.action_log += [a]
        # print(a, self.enabled_actions)
        # take an action

        if self.enabled_actions['cooling_storage']:
            _electric_demand_cooling = self.set_storage_cooling(a[0])
            a = a[1:]
        else:
            _electric_demand_cooling = self.set_storage_cooling()

        if self.enabled_actions['dhw_storage']:
            _electric_demand_dhw = self.set_storage_heating(a[0])
            a = a[1:]
        else:
            _electric_demand_dhw = self.set_storage_heating()

        if self.enabled_actions['pv_curtail']:
            self.solar_generation = self.get_solar_power(a[0])
            self.action_curtail = a[0]
            a = a[1:]
        else:
            self.solar_generation = self.get_solar_power()

        if self.enabled_actions['pv_phi']:
            self.phi = self.set_phase_lag(a[0])
            self.action_angle = a[0]
            a = a[1:]
        else:
            self.phi = self.set_phase_lag()

        if self.enabled_actions['electrical_storage']:
            self.battery_action
            _batt_power = self.set_storage_electrical(a[0]) # batt power is negative for discharge
            a = a[1:]
        else:
            _batt_power = self.set_storage_electrical()

        # Electrical appliances
        _non_shiftable_load = self.get_non_shiftable_load()

        # Adding loads from appliances and subtracting solar generation to the net electrical load of each building
        # print(_solar_generation, phi, _non_shiftable_load, _electric_demand_dhw, _electric_demand_cooling)
        self.current_gross_electricity_demand = round(_electric_demand_cooling + _electric_demand_dhw + _non_shiftable_load + max(_batt_power, 0), 4)
        self.current_gross_generation = round(self.solar_generation + min(0, _batt_power), 3)
        self.pv_log += [self.solar_generation]
        self.dhwload_log += [_electric_demand_dhw]
        self.hvacload_log += [_electric_demand_cooling]
        self.unshiftload_log += [_non_shiftable_load]
        # obs = self.get_obs()
        # reward = self.get_reward(obs)
        # done = False
        # info = {}
        self.time_step = (self.time_step + 1) % (8760 * self.hourly_timesteps)

        # # Assign the load in MW (from KW in CityLearn)
        # net.load.at[self.load_index, 'p_mw'] = 0.9 * self.current_gross_electricity_demand * 0.001
        # net.load.at[self.load_index, 'sn_mva'] = self.current_gross_electricity_demand * 0.001
        #
        # if self.gen_index > -1: # assume PV and battery are both behind the inverter
        #     net.sgen.at[self.gen_index, 'p_mw'] = -1 * self.current_gross_generation * np.cos(self.phi) * 0.001
        #     net.sgen.at[self.gen_index, 'q_mvar'] = self.current_gross_generation * np.sin(self.phi) * 0.001

        return #net

    def set_dhw_draws(self):
        self.sim_results['dhw_demand'] = subhourly_randomdraw_interp(self.sim_results['dhw_demand'], self.hourly_timesteps, self.dhw_heating_device.nominal_power)

    def autosize_equipment(self):
        # Autosize guarantees that the DHW device is large enough to always satisfy the maximum DHW demand
        if self.dhw_heating_device.nominal_power == 'autosize':

            # If the DHW device is a HeatPump
            if isinstance(self.dhw_heating_device, HeatPump):

                #We assume that the heat pump is always large enough to meet the highest heating or cooling demand of the building
                self.dhw_heating_device.nominal_power = np.array(self.sim_results['dhw_demand']/self.dhw_heating_device.cop_heating).max()

                # If the device is an electric heater
            elif isinstance(self.dhw_heating_device, ElectricHeater):
                    self.dhw_heating_device.nominal_power = (np.array(self.sim_results['dhw_demand'])/self.dhw_heating_device.efficiency).max()

        # Autosize guarantees that the cooling device device is large enough to always satisfy the maximum DHW demand
        if self.cooling_device.nominal_power == 'autosize':

            self.cooling_device.nominal_power = (np.array(self.sim_results['cooling_demand'])/ self.cooling_device.cop_cooling).max()

        # Defining the capacity of the storage devices as a number of times the maximum demand
        self.dhw_storage.capacity = max(self.sim_results['dhw_demand'])*self.dhw_storage.capacity
        self.cooling_storage.capacity = max(self.sim_results['cooling_demand'])*self.cooling_storage.capacity

        # Done in order to avoid dividing by 0 if the capacity is 0
        if self.dhw_storage.capacity <= 0.00001:
            self.dhw_storage.capacity = 0.00001
        if self.cooling_storage.capacity <= 0.00001:
            self.cooling_storage.capacity = 0.00001

    def set_state_space(self):
        # Finding the max and min possible values of all the states, which can then be used by the RL agent to scale the states and train any function approximators more effectively
        s_low, s_high = [], []
        for state_name, value in self.enabled_states.items():
            if value == True:
                if state_name == "net_electricity_consumption":
                    # lower and upper bounds of net electricity consumption are rough estimates and may not be completely accurate. Scaling this state-variable using these bounds may result in normalized values above 1 or below 0.
                    self._net_elec_cons_upper_bound = max(np.array(self.sim_results['non_shiftable_load']) - np.array(self.sim_results['solar_gen']) + np.array(self.sim_results['dhw_demand'])/.8 + np.array(self.sim_results['cooling_demand']) + self.dhw_storage.capacity/.8 + self.cooling_storage.capacity/2)
                    s_low.append(self.solar_power_capacity)
                    s_high.append(self._net_elec_cons_upper_bound)
                    self.net_elec_cons_range = self._net_elec_cons_upper_bound
                    self.net_elec_cons_mid = self.solar_power_capacity + 0.5 * self.net_elec_cons_range

                elif state_name == "absolute_voltage":
                    s_low.append(0.90)
                    s_high.append(1.10)

                elif state_name == "relative_voltage":
                    # @akp, added relative voltage to give homes their voltage ranked against the community max/min
                    s_low.append(0.) # the house is the lowest voltage in the community
                    s_high.append(1.)

                elif state_name == "total_voltage_spread":
                    # @akp, added total voltage spread to give a sense of the total loss incurred by the community. without the total voltage spread state "relative_voltage" is more or less meaningless. (total_voltage_spread = how much the community is penaltized, relative_voltage = what that house can do to fix the issue)
                    s_low.append(0.)
                    s_high.append(0.2)

                elif state_name in ['cooling_storage_soc','dhw_storage_soc','electrical_storage_soc']:
                    s_low.append(0.0)
                    s_high.append(1.0)

                else:
                    s_low.append(min(self.sim_results[state_name]))
                    s_high.append(max(self.sim_results[state_name]))

        self.normalization_range = np.array(s_high) - np.array(s_low)
        self.normalization_mid = np.array(s_low) + 0.5 * self.normalization_range

        num_states = sum(self.enabled_states.values())
        low = -1 * np.ones(num_states)
        high = np.ones(num_states)

        self.observation_space = spaces.Box(low=low, high=high, dtype=np.float32)
        return

    def set_action_space(self):
        # Setting the action space and the lower and upper bounds of each action-variable
        '''The energy storage (tank) capacity indicates how many times bigger the tank is compared to the maximum hourly energy demand of the building (cooling or DHW respectively), which sets a lower bound for the action of 1/tank_capacity, as the energy storage device can't provide the building with more energy than it will ever need for a given hour. The heat pump is sized using approximately the maximum hourly energy demand of the building (after accounting for the COP, see function autosize). Therefore, we make the fair assumption that the action also has an upper bound equal to 1/tank_capacity. This boundaries should speed up the learning process of the agents and make them more stable rather than if we just set them to -1 and 1. I.e. if Chilled_Water_Tank.Capacity is 3 (3 times the max. hourly demand of the building in the entire year), its actions will be bounded between -1/3 and 1/3'''
        a_low, a_high = [], []
        for action_name, value in self.enabled_actions.items():
            if value == True:
                if action_name =='cooling_storage':
                    # Avoid division by 0
                    if self.cooling_storage.capacity > 0.000001:
                        a_low.append(max(-1.0/self.cooling_storage.capacity, -1.0))
                        a_high.append(min(1.0/self.cooling_storage.capacity, 1.0))
                    else:
                        a_low.append(-1.0)
                        a_high.append(1.0)

                elif action_name == 'dhw_storage':
                    if self.dhw_storage.capacity > 0.000001:
                        a_low.append(max(-1.0/self.dhw_storage.capacity, -1.0))
                        a_high.append(min(1.0/self.dhw_storage.capacity, 1.0))
                    else:
                        a_low.append(-1.0)
                        a_high.append(1.0)

                elif action_name == 'pv_curtail':
                    # pv curtailment of apparent power, S
                    a_low.append(-1.0)
                    a_high.append(1.0)

                elif action_name == 'pv_phi':
                    # smart inverter voltage control @constance?
                    a_low.append(-1.0)
                    a_high.append(1.0)

                elif action_name == 'electrical_storage':
                    a_low.append(-1.0)
                    a_high.append(1.0)

        self.action_space = spaces.Box(low=np.array(a_low), high=np.array(a_high), dtype=np.float32)
        return

    def set_storage_electrical(self, action=0):
        """
        Args:
            action (float): Amount of heating energy stored (added) in that time-step as a ratio of the maximum capacity of the energy storage device.
            -1 =< action < 0 : Energy Storage Unit releases energy into the building and its State of Charge decreases
            0 < action <= 1 : Energy Storage Unit receives energy from the energy supply device and its State of Charge increases
            The actions are always subject to the constraints of the power capacity of the heating supply unit, the DHW demand of the
            building (which limits the maximum amount of DHW that the energy storage can provide to the building), and the state of charge of the
            energy storage unit itself
        Return:
            elec_demand_heating (float): electricity consumption needed for space heating and heating storage
        """

        electrical_energy_balance = self.electrical_storage.charge(action*self.electrical_storage.capacity)

        if self.save_memory == False:
            self.electrical_storage_electric_consumption.append(electrical_energy_balance)
            self.electrical_storage_soc.append(self.electrical_storage._soc)

        self.electrical_storage.time_step += 1

        return electrical_energy_balance

    def set_storage_heating(self, action=0):
        """
        Args:
            action (float): Amount of heating energy stored (added) in that time-step as a ratio of the maximum capacity of the energy storage device.
            -1 =< action < 0 : Energy Storage Unit releases energy into the building and its State of Charge decreases
            0 < action <= 1 : Energy Storage Unit receives energy from the energy supply device and its State of Charge increases
            The actions are always subject to the constraints of the power capacity of the heating supply unit, the DHW demand of the
            building (which limits the maximum amount of DHW that the energy storage can provide to the building), and the state of charge of the
            energy storage unit itself
        Return:
            elec_demand_heating (float): electricity consumption needed for space heating and heating storage
        """

        # Heating power that could be possible to supply to the storage device to increase its State of Charge once the heating demand of the building has been satisfied
        heat_power_avail = self.dhw_heating_device.get_max_heating_power() - self.sim_results['dhw_demand'][self.time_step]

        # The storage device is charged (action > 0) or discharged (action < 0) taking into account the max power available and that the storage device cannot be discharged by an amount of energy greater than the energy demand of the building.
        heating_energy_balance = self.dhw_storage.charge(max(-self.sim_results['dhw_demand'][self.time_step], min(heat_power_avail, action*self.dhw_storage.capacity)))

        if self.save_memory == False:
            self.dhw_storage_action.append(action)
            self.dhw_heating_device_to_storage.append(max(0, heating_energy_balance))
            self.dhw_storage_to_building.append(-min(0, heating_energy_balance))
            self.dhw_heating_device_to_building.append(self.sim_results['dhw_demand'][self.time_step] + min(0, heating_energy_balance))
            self.dhw_storage_soc.append(self.dhw_storage._soc)

        # The energy that the energy supply device must provide is the sum of the energy balance of the storage unit (how much net energy it will lose or get) plus the energy supplied to the building. A constraint is added to guarantee it's always positive.
        heating_energy_balance = max(0, heating_energy_balance + self.sim_results['dhw_demand'][self.time_step])

        # Electricity consumed by the energy supply unit
        elec_demand_heating = self.dhw_heating_device.set_total_electric_consumption_heating(heat_supply = heating_energy_balance)

        # Electricity consumption used (if +) or saved (if -) due to the change in the state of charge of the energy storage device
        self._electric_consumption_dhw_storage = elec_demand_heating - self.dhw_heating_device.get_electric_consumption_heating(heat_supply = self.sim_results['dhw_demand'][self.time_step])

        if self.save_memory == False:
            self.electric_consumption_dhw.append(elec_demand_heating)
            self.electric_consumption_dhw_storage.append(self._electric_consumption_dhw_storage)

        self.dhw_heating_device.time_step += 1

        return elec_demand_heating

    def set_storage_cooling(self, action=0):
        """
            Args:
                action (float): Amount of cooling energy stored (added) in that time-step as a ratio of the maximum capacity of the energy storage device.
                1 =< action < 0 : Energy Storage Unit releases energy into the building and its State of Charge decreases
                0 < action <= -1 : Energy Storage Unit receives energy from the energy supply device and its State of Charge increases
                The actions are always subject to the constraints of the power capacity of the cooling supply unit, the cooling demand of the
                building (which limits the maximum amount of cooling energy that the energy storage can provide to the building), and the state of charge of the energy storage unit itself
            Return:
                elec_demand_cooling (float): electricity consumption needed for space cooling and cooling storage
        """

        # Cooling power that could be possible to supply to the storage device to increase its State of Charge once the heating demand of the building has been satisfied
        cooling_power_avail = self.cooling_device.get_max_cooling_power() - self.sim_results['cooling_demand'][self.time_step]

        # The storage device is charged (action > 0) or discharged (action < 0) taking into account the max power available and that the storage device cannot be discharged by an amount of energy greater than the energy demand of the building.
        charge_arg = max(-self.sim_results['cooling_demand'][self.time_step], min(cooling_power_avail, action*self.cooling_storage.capacity))
        cooling_energy_balance = self.cooling_storage.charge(charge_arg/self.hourly_timesteps) # @AKP FIX THIS -- make less janky!!!

        if self.save_memory == False:
            self.cooling_storage_action.append(action)
            self.cooling_device_to_storage.append(max(0, cooling_energy_balance))
            self.cooling_storage_to_building.append(-min(0, cooling_energy_balance))
            self.cooling_device_to_building.append(self.sim_results['cooling_demand'][self.time_step] + min(0, cooling_energy_balance))
            self.cooling_storage_soc.append(self.cooling_storage._soc)

        # The energy that the energy supply device must provide is the sum of the energy balance of the storage unit (how much net energy it will lose or get) plus the energy supplied to the building. A constraint is added to guarantee it's always positive.
        cooling_energy_balance = max(0, cooling_energy_balance + self.sim_results['cooling_demand'][self.time_step])

        # Electricity consumed by the energy supply unit
        elec_demand_cooling = self.cooling_device.set_total_electric_consumption_cooling(cooling_supply = cooling_energy_balance)

        # Electricity consumption used (if +) or saved (if -) due to the change in the state of charge of the energy storage device
        self._electric_consumption_cooling_storage = elec_demand_cooling - self.cooling_device.get_electric_consumption_cooling(cooling_supply = self.sim_results['cooling_demand'][self.time_step])

        if self.save_memory == False:
            self.electric_consumption_cooling.append(np.float32(elec_demand_cooling))
            self.electric_consumption_cooling_storage.append(np.float32(self._electric_consumption_cooling_storage))

        self.cooling_device.time_step += 1

        return elec_demand_cooling

    def get_non_shiftable_load(self):
        return self.sim_results['non_shiftable_load'][self.time_step]

    def get_solar_power(self, curtailment=1):
        c = 0.5 - 0.5 * curtailment # maps curtailment -1 to 100% reduction and 1 to no curtailment
        self.solar_power = (1 - c) * self.sim_results['solar_gen'][self.time_step]
        return 5*self.solar_power

    def set_phase_lag(self,phi=-1):
        # mapping to that -1 is 0 and 1 in np.pi/2
        phi = (phi+1)*np.pi/4
        self.v_lag = phi
        return self.v_lag

    def set_dhw_cop(self):
        # If the DHW device is a HeatPump
        if isinstance(self.dhw_heating_device, HeatPump):
            # Calculating COPs of the heat pumps for every hour
            self.dhw_heating_device.cop_heating = self.dhw_heating_device.eta_tech*(self.dhw_heating_device.t_target_heating + 273.15)/np.clip(self.dhw_heating_device.t_target_heating - self.sim_results['t_out'],a_min=0.1*np.ones(len(self.sim_results['t_out'])), a_max=None)
            self.dhw_heating_device.cop_heating[self.dhw_heating_device.cop_heating < 0] = 20.0
            self.dhw_heating_device.cop_heating[self.dhw_heating_device.cop_heating > 20] = 20.0
            self.dhw_heating_device.cop_heating = self.dhw_heating_device.cop_heating.to_numpy()

    def get_dhw_electric_demand(self):
        return self.dhw_heating_device._electrical_consumption_heating

    def set_cooling_cop(self):
        self.cooling_device.cop_cooling = self.cooling_device.eta_tech*(np.add(self.cooling_device.t_target_cooling,273.15))/np.clip(np.subtract(self.sim_results['t_out'],self.cooling_device.t_target_cooling),a_min=0.1*np.ones(len(self.sim_results['t_out'])), a_max=None)
        self.cooling_device.cop_cooling[self.cooling_device.cop_cooling < 0] = 20.0
        self.cooling_device.cop_cooling[self.cooling_device.cop_cooling > 20] = 20.0

    def get_cooling_electric_demand(self):
        return self.cooling_device._electrical_consumption_cooling

    def reset_timestep(self, net):
        self.time_step = 0
        return self.reset(net)

    def reset(self, net):

        self.current_gross_electricity_demand = self.sim_results['non_shiftable_load'][self.time_step]
        self.current_gross_generation = self.sim_results['solar_gen'][self.time_step]

        if self.dhw_storage is not None:
            self.dhw_storage.reset()
        if self.cooling_storage is not None:
            self.cooling_storage.reset()
        if self.electrical_storage is not None:
            self.electrical_storage.reset()
        if self.dhw_heating_device is not None:
            self.dhw_heating_device.reset()
            self.current_gross_electricity_demand += self.dhw_heating_device.get_electric_consumption_heating(self.sim_results['dhw_demand'][self.time_step])
        if self.cooling_device is not None:
            self.cooling_device.reset()
            self.current_gross_electricity_demand += self.cooling_device.get_electric_consumption_cooling(self.sim_results['cooling_demand'][self.time_step])

        self._electric_consumption_cooling_storage = 0.0
        self._electric_consumption_dhw_storage = 0.0

        self.cooling_demand_building = []
        self.dhw_demand_building = []
        self.electric_consumption_appliances = []
        self.electric_generation = []

        self.electric_consumption_cooling = []
        self.electric_consumption_cooling_storage = []
        self.electric_consumption_dhw = []
        self.electric_consumption_dhw_storage = []

        self.net_electric_consumption = []
        self.net_electric_consumption_no_storage = []
        self.net_electric_consumption_no_pv_no_storage = []

        self.cooling_storage_action = []
        self.cooling_device_to_building = []
        self.cooling_storage_to_building = []
        self.cooling_device_to_storage = []
        self.cooling_storage_soc = []

        self.dhw_storage_action = []
        self.dhw_heating_device_to_building = []
        self.dhw_storage_to_building = []
        self.dhw_heating_device_to_storage = []
        self.dhw_storage_soc = []

        self.electrical_storage_electric_consumption = []
        self.electrical_storage_soc = []
        return self.get_obs(net)

    def terminate(self):

        if self.dhw_storage is not None:
            self.dhw_storage.terminate()
        if self.cooling_storage is not None:
            self.cooling_storage.terminate()
        if self.electrical_storage is not None:
            self.electrical_storage.terminate()
        if self.dhw_heating_device is not None:
            self.dhw_heating_device.terminate()
        if self.cooling_device is not None:
            self.cooling_device.terminate()

        if self.save_memory == False:

            self.cooling_demand_building = np.array(self.sim_results['cooling_demand'][:self.time_step])
            self.dhw_demand_building = np.array(self.sim_results['dhw_demand'][:self.time_step])
            self.electric_consumption_appliances = np.array(self.sim_results['non_shiftable_load'][:self.time_step])
            self.electric_generation = np.array(self.sim_results['solar_gen'][:self.time_step])

            elec_consumption_dhw = 0
            elec_consumption_dhw_storage = 0
            if self.dhw_heating_device.time_step == self.time_step and self.dhw_heating_device is not None:
                elec_consumption_dhw = np.array(self.electric_consumption_dhw)
                elec_consumption_dhw_storage = np.array(self.electric_consumption_dhw_storage)

            elec_consumption_cooling = 0
            elec_consumption_cooling_storage = 0
            if self.cooling_device.time_step == self.time_step and self.cooling_device is not None:
                elec_consumption_cooling = np.array(self.electric_consumption_cooling)
                elec_consumption_cooling_storage = np.array(self.electric_consumption_cooling_storage)

            self.net_electric_consumption = np.array(self.electric_consumption_appliances) + elec_consumption_cooling + elec_consumption_dhw - np.array(self.electric_generation)
            self.net_electric_consumption_no_storage = np.array(self.electric_consumption_appliances) + (elec_consumption_cooling - elec_consumption_cooling_storage) + (elec_consumption_dhw - elec_consumption_dhw_storage) - np.array(self.electric_generation)
            self.net_electric_consumption_no_pv_no_storage = np.array(self.net_electric_consumption_no_storage) + np.array(self.electric_generation)

            self.cooling_demand_building = np.array(self.cooling_demand_building)
            self.dhw_demand_building = np.array(self.dhw_demand_building)
            self.electric_consumption_appliances = np.array(self.electric_consumption_appliances)
            self.electric_generation = np.array(self.electric_generation)

            self.electric_consumption_cooling = np.array(self.electric_consumption_cooling)
            self.electric_consumption_cooling_storage = np.array(self.electric_consumption_cooling_storage)
            self.electric_consumption_dhw = np.array(self.electric_consumption_dhw)
            self.electric_consumption_dhw_storage = np.array(self.electric_consumption_dhw_storage)

            self.net_electric_consumption = np.array(self.net_electric_consumption)
            self.net_electric_consumption_no_storage = np.array(self.net_electric_consumption_no_storage)
            self.net_electric_consumption_no_pv_no_storage = np.array(self.net_electric_consumption_no_pv_no_storage)

            self.cooling_device_to_building = np.array(self.cooling_device_to_building)
            self.cooling_storage_to_building = np.array(self.cooling_storage_to_building)
            self.cooling_device_to_storage = np.array(self.cooling_device_to_storage)
            self.cooling_storage_soc = np.array(self.cooling_storage_soc)

            self.dhw_heating_device_to_building = np.array(self.dhw_heating_device_to_building)
            self.dhw_storage_to_building = np.array(self.dhw_storage_to_building)
            self.dhw_heating_device_to_storage = np.array(self.dhw_heating_device_to_storage)
            self.dhw_storage_soc = np.array(self.dhw_storage_soc)

            self.electrical_storage_electric_consumption = np.array(self.electrical_storage_electric_consumption)
            self.electrical_storage_soc = np.array(self.electrical_storage_soc)

class HeatPump:
    def __init__(self, nominal_power = None, eta_tech = None, t_target_heating = None, t_target_cooling = None, save_memory = True):
        """
        Args:
            nominal_power (float): Maximum amount of electric power that the heat pump can consume from the power grid (given by the nominal power of the compressor)
            eta_tech (float): Technical efficiency
            t_target_heating (float): Temperature at which the heating energy is released
            t_target_cooling (float): Temperature at which the cooling energy is released
        """
        #Parameters
        self.nominal_power = nominal_power
        self.eta_tech = eta_tech

        #Variables
        self.max_cooling = None
        self.max_heating = None
        self._cop_heating = None
        self._cop_cooling = None
        self.t_target_heating = t_target_heating
        self.t_target_cooling = t_target_cooling
        self.t_source_heating = None
        self.t_source_cooling = None
        self.cop_heating = []
        self.cop_cooling = []
        self.electrical_consumption_cooling = []
        self.electrical_consumption_heating = []
        self.heat_supply = []
        self.cooling_supply = []
        self.time_step = 0
        self.save_memory = save_memory

    def get_max_cooling_power(self, max_electric_power = None):
        """
        Args:
            max_electric_power (float): Maximum amount of electric power that the heat pump can consume from the power grid

        Returns:
            max_cooling (float): maximum amount of cooling energy that the heatpump can provide
        """

        if max_electric_power is None:
            self.max_cooling = self.nominal_power*self.cop_cooling[self.time_step]
        else:
            self.max_cooling = min(max_electric_power, self.nominal_power)*self.cop_cooling[self.time_step]
        return self.max_cooling

    def get_max_heating_power(self, max_electric_power = None):
        """
        Method that calculates the heating COP and the maximum heating power available
        Args:
            max_electric_power (float): Maximum amount of electric power that the heat pump can consume from the power grid

        Returns:
            max_heating (float): maximum amount of heating energy that the heatpump can provide
        """

        if max_electric_power is None:
            self.max_heating = self.nominal_power*self.cop_cooling[self.time_step]
        else:
            self.max_heating = min(max_electric_power, self.nominal_power)*self.cop_cooling[self.time_step]

        return self.max_heating

    def set_total_electric_consumption_cooling(self, cooling_supply = 0):
        """
        Method that calculates the total electricity consumption of the heat pump given an amount of cooling energy to be supplied to both the building and the storage unit
        Args:
            cooling_supply (float): Total amount of cooling energy that the heat pump is going to supply

        Returns:
            _electrical_consumption_cooling (float): electricity consumption for cooling
        """

        self.cooling_supply.append(cooling_supply)
        self._electrical_consumption_cooling = cooling_supply/self.cop_cooling[self.time_step]

        if self.save_memory == False:
            self.electrical_consumption_cooling.append(np.float32(self._electrical_consumption_cooling))

        return self._electrical_consumption_cooling

    def get_electric_consumption_cooling(self, cooling_supply = 0):
        """
        Method that calculates the electricity consumption of the heat pump given an amount of cooling energy
        Args:
            cooling_supply (float): Amount of cooling energy

        Returns:
            _electrical_consumption_cooling (float): electricity consumption for that amount of cooling
        """

        _elec_consumption_cooling = cooling_supply/self.cop_cooling[self.time_step]
        return _elec_consumption_cooling

    def set_total_electric_consumption_heating(self, heat_supply = 0):
        """
        Method that calculates the electricity consumption of the heat pump given an amount of heating energy to be supplied
        Args:
            heat_supply (float): Amount of heating energy that the heat pump is going to supply

        Returns:
            _elec_consumption_heating (float): electricity consumption for heating
        """

        self.heat_supply.append(heat_supply)
        self._electrical_consumption_heating = heat_supply/self.cop_heating[self.time_step]

        if self.save_memory == False:
            self.electrical_consumption_heating.append(np.float32(self._electrical_consumption_heating))

        return self._electrical_consumption_heating

    def get_electric_consumption_heating(self, heat_supply = 0):
        """
        Method that calculates the electricity consumption of the heat pump given an amount of heating energy to be supplied
        Args:
            heat_supply (float): Amount of heating energy that the heat pump is going to supply

        Returns:
            _elec_consumption_heating (float): electricity consumption for heating
        """

        _elec_consumption_heating = heat_supply/self.cop_heating[self.time_step]
        return _elec_consumption_heating

    def reset(self):
        self.t_source_heating = None
        self.t_source_cooling = None
        self.max_cooling = None
        self.max_heating = None
        self._cop_heating = None
        self._cop_cooling = None
        self._electrical_consumption_cooling = 0
        self._electrical_consumption_heating = 0
        self.electrical_consumption_cooling = []
        self.electrical_consumption_heating = []
        self.heat_supply = []
        self.cooling_supply = []
        self.time_step = 0

    def terminate(self):
        if self.save_memory == False:
            self.cop_heating = self.cop_heating[:self.time_step]
            self.cop_cooling = self.cop_cooling[:self.time_step]
            self.electrical_consumption_cooling = np.array(self.electrical_consumption_cooling)
            self.electrical_consumption_heating = np.array(self.electrical_consumption_heating)
            self.heat_supply = np.array(self.heat_supply)
            self.cooling_supply = np.array(self.cooling_supply)

class ElectricHeater:
    def __init__(self, nominal_power = None, efficiency = None, save_memory = True):
        """
        Args:
            nominal_power (float): Maximum amount of electric power that the electric heater can consume from the power grid
            efficiency (float): efficiency
        """

        #Parameters
        self.nominal_power = nominal_power
        self.efficiency = efficiency

        #Variables
        self.max_heating = None
        self.electrical_consumption_heating = []
        self._electrical_consumption_heating = 0
        self.heat_supply = []
        self.time_step = 0
        self.save_memory = save_memory

    def terminate(self):
        if self.save_memory == False:
            self.electrical_consumption_heating = np.array(self.electrical_consumption_heating)
            self.heat_supply = np.array(self.heat_supply)

    def get_max_heating_power(self, max_electric_power = None, t_source_heating = None, t_target_heating = None):
        """Method that calculates the maximum heating power available
        Args:
            max_electric_power (float): Maximum amount of electric power that the electric heater can consume from the power grid
            t_source_heating (float): Not used by the electric heater
            t_target_heating (float): Not used by electric heater

        Returns:
            max_heating (float): maximum amount of heating energy that the electric heater can provide
        """

        if max_electric_power is None:
            self.max_heating = self.nominal_power*self.efficiency
        else:
            self.max_heating = self.max_electric_power*self.efficiency

        return self.max_heating

    def set_total_electric_consumption_heating(self, heat_supply = 0):
        """
        Args:
            heat_supply (float): Amount of heating energy that the electric heater is going to supply

        Returns:
            _electrical_consumption_heating (float): electricity consumption for heating
        """

        self.heat_supply.append(heat_supply)
        self._electrical_consumption_heating = heat_supply/self.efficiency

        if self.save_memory == False:
            self.electrical_consumption_heating.append(np.float32(self._electrical_consumption_heating))

        return self._electrical_consumption_heating

    def get_electric_consumption_heating(self, heat_supply = 0):
        """
        Args:
            heat_supply (float): Amount of heating energy that the electric heater is going to supply

        Returns:
            _electrical_consumption_heating (float): electricity consumption for heating
        """

        _electrical_consumption_heating = heat_supply/self.efficiency
        return _electrical_consumption_heating

    def reset(self):
        self.max_heating = None
        self.electrical_consumption_heating = []
        self.heat_supply = []

class EnergyStorage:
    def __init__(self,  hourly_steps, capacity = None, max_power_output = None, max_power_charging = None, efficiency = 1, loss_coeff = 0, save_memory = True):
        """
        Generic energy storage class. It can be used as a chilled water storage tank or a DHW storage tank
        Args:
            capacity (float): Maximum amount of energy that the storage unit is able to store (kWh)
            max_power_output (float): Maximum amount of power that the storage unit can output (kW)
            max_power_charging (float): Maximum amount of power that the storage unit can use to charge (kW)
            efficiency (float): Efficiency factor of charging and discharging the storage unit (from 0 to 1)
            loss_coeff (float): Loss coefficient used to calculate the amount of energy lost every hour (from 0 to 1)
        """

        self.capacity = capacity
        self.max_power_output = max_power_output
        self.max_power_charging = max_power_charging
        self.efficiency = efficiency**0.5
        self.loss_coeff = loss_coeff
        self.soc = []
        self._soc = np.random.uniform(0.2*self.capacity, 0.8*self.capacity)#0 # State of Charge
        self.energy_balance = []
        self._energy_balance = 0
        self.save_memory = save_memory
        self.hourly_steps = hourly_steps

    def terminate(self):
        if self.save_memory == False:
            self.energy_balance = np.array(self.energy_balance)
            self.soc =  np.array(self.soc)

    def charge(self, energy):
        """Method that controls both the energy CHARGE and DISCHARGE of the energy storage device
        energy < 0 -> Discharge
        energy > 0 -> Charge
        Args:
            energy (float): Amount of energy stored in that time-step (Wh)
        Return:
            energy_balance (float):
        """

        #The initial State Of Charge (SOC) is the previous SOC minus the energy losses
        soc_init = self._soc*(1-self.loss_coeff)

        #Charging
        if energy >= 0:
            if self.max_power_charging is not None:
                energy =  min(energy, self.max_power_charging)
            self._soc = soc_init + energy*self.efficiency/self.hourly_steps

        #Discharging
        else:
            if self.max_power_output is not None:
                energy = max(-max_power_output, energy)
            self._soc = max(0, soc_init + energy/self.efficiency/self.hourly_steps)

        if self.capacity is not None:
            self._soc = min(self._soc, self.capacity)

        # Calculating the energy balance with its external environmrnt (amount of energy taken from or relseased to the environment)

        #Charging
        if energy >= 0:
            self._energy_balance = (self._soc - soc_init)/self.efficiency

        #Discharging
        else:
            self._energy_balance = (self._soc - soc_init)*self.efficiency

        if self.save_memory == False:
            self.energy_balance.append(np.float32(self._energy_balance))
            self.soc.append(np.float32(self._soc))

        # print(energy, self._energy_balance)
        return self._energy_balance

    def reset(self):
        self.soc = []
        self._soc = np.random.uniform(0.2*self.capacity,0.8*self.capacity)#0 #State of charge
        self.energy_balance = [] #Positive for energy entering the storage
        self._energy_balance = 0
        self.time_step = 0

class Battery:
    def __init__(self, hourly_timesteps, capacity, nominal_power = None, capacity_loss_coeff = None, power_efficiency_curve = None, capacity_power_curve = None, efficiency = None, loss_coeff = 0, save_memory = True):
        """
        Generic energy storage class. It can be used as a chilled water storage tank or a DHW storage tank
        Args:
            capacity (float): Maximum amount of energy that the storage unit is able to store (kWh)
            max_power_charging (float): Maximum amount of power that the storage unit can use to charge (kW)
            efficiency (float): Efficiency factor of charging and discharging the storage unit (from 0 to 1)
            loss_coeff (float): Loss coefficient used to calculate the amount of energy lost every hour (from 0 to 1)
            power_efficiency_curve (float): Charging/Discharging efficiency as a function of the power released or consumed
            capacity_power_curve (float): Max. power of the battery as a function of its current state of charge
            capacity_loss_coeff (float): Battery degradation. Storage capacity lost in each charge and discharge cycle (as a fraction of the total capacity)
        """

        self.capacity = capacity
        self.c0 = capacity
        self.nominal_power = nominal_power
        self.capacity_loss_coeff = capacity_loss_coeff

        if power_efficiency_curve is not None:
            self.power_efficiency_curve = np.array(power_efficiency_curve).T
        else:
            self.power_efficiency_curve = power_efficiency_curve

        if capacity_power_curve is not None:
            self.capacity_power_curve = np.array(capacity_power_curve).T
        else:
            self.capacity_power_curve = capacity_power_curve

        self.efficiency = efficiency**0.5
        self.loss_coeff = loss_coeff
        self.max_power = None
        self._eff = []
        self._energy = []
        self._max_power = []
        self.soc = []
        self._soc = np.random.uniform(0.2*self.capacity, 0.8*self.capacity)#0 # State of Charge
        self.energy_balance = []
        self._energy_balance = 0
        self.save_memory = save_memory
        self.time_step = 0
        self.hourly_timesteps = hourly_timesteps

    def terminate(self):
        if self.save_memory == False:
            self.energy_balance = np.array(self.energy_balance)
            self.soc =  np.array(self.soc)

    def charge(self, energy):
        """Method that controls both the energy CHARGE and DISCHARGE of the energy storage device
        energy < 0 -> Discharge
        energy > 0 -> Charge
        Args:
            energy (float): Amount of energy stored in that time-step (Wh)
        Return:
            energy_balance (float):
        """

        #The initial State Of Charge (SOC) is the previous SOC minus the energy losses
        soc_init = self._soc*(1-self.loss_coeff)
        if self.capacity_power_curve is not None:
            soc_normalized = soc_init/self.capacity
            # Calculating the maximum power rate at which the battery can be charged or discharged
            idx = max(0, np.argmax(soc_normalized <= self.capacity_power_curve[0]) - 1)

            self.max_power = self.nominal_power*(self.capacity_power_curve[1][idx] + (self.capacity_power_curve[1][idx+1] - self.capacity_power_curve[1][idx]) * (soc_normalized - self.capacity_power_curve[0][idx])/(self.capacity_power_curve[0][idx+1] - self.capacity_power_curve[0][idx]))

        else:
            self.max_power = self.nominal_power

        #Charging
        if energy >= 0:
            if self.nominal_power is not None:

                energy =  min(energy, self.max_power)
                if self.power_efficiency_curve is not None:
                    # Calculating the maximum power rate at which the battery can be charged or discharged
                    energy_normalized = np.abs(energy)/self.nominal_power
                    idx = max(0, np.argmax(energy_normalized <= self.power_efficiency_curve[0]) - 1)
                    self.efficiency = self.power_efficiency_curve[1][idx] + (energy_normalized - self.power_efficiency_curve[0][idx])*(self.power_efficiency_curve[1][idx + 1] - self.power_efficiency_curve[1][idx])/(self.power_efficiency_curve[0][idx + 1] - self.power_efficiency_curve[0][idx])
                    self.efficiency = self.efficiency**0.5

            self._soc = soc_init + energy*self.efficiency/self.hourly_timesteps

        #Discharging
        else:
            if self.nominal_power is not None:
                energy = max(-self.max_power, energy)

            if self.power_efficiency_curve is not None:

                # Calculating the maximum power rate at which the battery can be charged or discharged
                energy_normalized = np.abs(energy)/self.nominal_power
                idx = max(0, np.argmax(energy_normalized <= self.power_efficiency_curve[0]) - 1)
                self.efficiency = self.power_efficiency_curve[1][idx] + (energy_normalized - self.power_efficiency_curve[0][idx])*(self.power_efficiency_curve[1][idx + 1] - self.power_efficiency_curve[1][idx])/(self.power_efficiency_curve[0][idx + 1] - self.power_efficiency_curve[0][idx])
                self.efficiency = self.efficiency**0.5

            self._soc = max(0, soc_init + energy/self.efficiency/self.hourly_timesteps)

        if self.capacity is not None:
            self._soc = min(self._soc, self.capacity)

        # Calculating the energy balance with its external environment (amount of energy taken from or relseased to the environment)

        #Charging
        if energy >= 0:
            self._energy_balance = (self._soc - soc_init)/self.efficiency

        #Discharging
        else:
            self._energy_balance = (self._soc - soc_init)*self.efficiency

        # Calculating the degradation of the battery: new max. capacity of the battery after charge/discharge
        self.capacity -= self.capacity_loss_coeff*self.c0*np.abs(self._energy_balance)/(2*self.capacity)

        if self.save_memory == False:
            self.energy_balance.append(np.float32(self._energy_balance))
            self.soc.append(np.float32(self._soc))

        return self._energy_balance

    def reset(self):
        self.soc = []
        self._soc = np.random.uniform(0.2*self.capacity,0.8*self.capacity) #State of charge
        self.energy_balance = [] #Positive for energy entering the storage
        self._energy_balance = 0
        self.time_step = 0
