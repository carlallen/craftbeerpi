from brewapp import app, socketio
from brewapp.base.actor import *
import datetime

KpHeat = 10
KpCool = 5
Ki = 0.02
KdCool = -5
KdHeat = -10
IDLE_RANGE_HIGH = 5
IDLE_RANGE_LOW = -5
MAX_HEAT_TIME_FOR_ESTIMATE = 600
MAX_COOL_TIME_FOR_ESTIMATE = 1200
HEATING_TARGET_UPPER = (+2)
HEATING_TARGET_LOWER = (-1)
COOLING_TARGET_UPPER = (+1)
COOLING_TARGET_LOWER = (-2)
COOLING_TARGET = ((COOLING_TARGET_UPPER+COOLING_TARGET_LOWER)/2)
HEATING_TARGET = ((HEATING_TARGET_UPPER+HEATING_TARGET_LOWER)/2)



class FermentationControl(object):
  def __init__(self, fermenterid):
    self.fermenterid = int(fermenterid)
    beer_temp = app.brewapp_thermometer_last[self.fermenter()["sensorid"]]
    chamber_temp = app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]]
    # State Variables
    self.state = "IDLE"
    self.do_neg_peak_detect = False;
    self.do_pos_peak_detect = False;
    # Calculated Temperature Setting
    self.set_chamber_target_temp(self.fermenter()["target_temp"])
    # Filtered Temperature Data
    self.chamber_temp = [chamber_temp] * 4
    self.chamber_temp_filt = [chamber_temp] * 4
    self.beer_temp = [beer_temp] * 4
    self.beer_temp_filt = [beer_temp] * 4
    self.beer_slope = 0
    # History for Slope Calculation
    self.beer_temp_history = [beer_temp] *30
    self.beer_temp_history_index = 0
    # Control parameters
    self.heat_overshoot_estimator = 0
    self.cool_overshoot_estimator = 0
    self.chamber_setting_for_neg_peak_estimate = 0
    self.chamber_setting_for_pos_peak_estimate = 0
    self.neg_peak = 0
    self.pos_peak = 0
    self.difference_integral = 0
    # Timers
    self.last_cool_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
    self.last_heat_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
    self.last_idle_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
    # PID Settings
    if self.fermenter()["target_temp"] < beer_temp:
      self.Kp = KpCool
      self.Kd = KdCool
    else:
      self.Kp = KpHeat
      self.Kd = KdHeat

  def fermenter(self):
    return app.cbp['FERMENTERS'][self.fermenterid]

  def set_chamber_target_temp(self, temp):
    self.chamber_target_temp = temp
    f = Fermenter.query.get(int(self.fermenterid))
    d = to_dict(f, deep={'steps': []})
    d["chamber_target_temp"] = temp
    app.cbp['FERMENTERS'][self.fermenterid] = d
    socketio.emit('fermenter_update', d, namespace='/brew')

  def update_settings(self):
    beer_temp_diff = self.fermenter()["target_temp"]-self.beer_temp_filt[3];
    if abs(beer_temp_diff) < 5 and ((self.beer_slope <= 0.7 and self.beer_slope >= 0) or (self.beer_slope >= -1.4 and self.beer_slope <= 0)):  # difference is smaller than .5 degree and slope is almost horizontal
      if abs(beer_temp_diff)> 0.5:
        self.difference_integral = self.difference_integral + beer_temp_diff;
    else:
      self.difference_integral = self.difference_integral * 0.9
    if beer_temp_diff < 0: #linearly go to cool parameters in 3 hours
      self.Kp = constrain(self.Kp+(KpCool-KpHeat)/(1080.0), KpCool, KpHeat)
      self.Kd = constrain(self.Kd+(KdCool-KdHeat)/(1080.0), KdHeat, KdCool)
    else: #linearly go to heat parameters in 3 hours
      self.Kp = constrain(self.Kp+(KpHeat-KpCool)/(1080.0), KpCool, KpHeat)
      self.Kd = constrain(self.Kd+(KdHeat-KdCool)/(1080.0), KdHeat, KdCool)
    self.set_chamber_target_temp(round(constrain(self.fermenter()["target_temp"] + self.Kp* beer_temp_diff + Ki* self.difference_integral + self.Kd*self.beer_slope, min_temp(), max_temp()), 2))

  def update_state(self):
    if self.state == "IDLE":
      self.last_idle_time = datetime.datetime.utcnow()
      if ((self.time_since_cooling() > datetime.timedelta(minutes=15) or not self.do_neg_peak_detect) and (self.time_since_heating() > datetime.timedelta(minutes=10)) or not self.do_pos_peak_detect): # if cooling is 15 min ago and heating 10
        if app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]] > self.chamber_target_temp + IDLE_RANGE_HIGH:
          if self.beer_temp_filt[3] > self.fermenter()["target_temp"] + 0.5: # only start cooling when beer is too warm (0.05 degree idle space)
            self.state = "COOLING"
          return True
        if app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]] < self.chamber_target_temp + IDLE_RANGE_LOW:
          if self.beer_temp_filt[3] < self.fermenter()["target_temp"] - 0.5: # only start heating when beer is too cold (0.05 degree idle space)
            self.state = "HEATING";
          return True
      if self.time_since_cooling() > datetime.timedelta(minutes=30):
        self.do_neg_peak_detect = False # peak would be from drifting in idle, not from cooling
      if self.time_since_heating() > datetime.timedelta(minutes=20):
        self.do_pos_peak_detect = False # peak would be from drifting in idle, not from heating
    elif self.state == "COOLING":
      self.do_neg_peak_detect = True
      self.last_cool_time = datetime.datetime.utcnow()
      estimated_overshoot = self.cool_overshoot_estimator * min(MAX_COOL_TIME_FOR_ESTIMATE, self.time_since_idle().total_seconds())/60;
      estimated_peak_temp = app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]] - estimated_overshoot;
      if estimated_peak_temp <= self.chamber_target_temp + COOLING_TARGET:
        self.chamber_setting_for_neg_peak_estimate = self.chamber_target_temp
        self.state="IDLE"
        return True
    elif self.state == "HEATING":
      self.do_pos_peak_detect = True
      self.last_heat_time = datetime.datetime.utcnow()
      estimated_overshoot = self.heat_overshoot_estimator * min(MAX_HEAT_TIME_FOR_ESTIMATE, self.time_since_idle().total_seconds())/60;
      estimated_peak_temp = app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]] + estimated_overshoot;
      if estimated_peak_temp >= self.chamber_target_temp + HEATING_TARGET:
        self.chamber_setting_for_pos_peak_estimate = self.chamber_target_temp
        self.state="IDLE"
        return True
    else:
      self.state = "IDLE" # should never happen

  def update_filtered_temperatures(self):
    # Input for filter
    self.chamber_temp[0] = self.chamber_temp[1]
    self.chamber_temp[1] = self.chamber_temp[2]
    self.chamber_temp[2] = self.chamber_temp[3];
    self.chamber_temp[3] = app.brewapp_thermometer_last[self.fermenter()["chambersensorid"]]

    # Butterworth filter with cutoff frequency 0.01*sample frequency (FS=0.1Hz)
    self.chamber_temp_filt[0] = self.chamber_temp_filt[1]
    self.chamber_temp_filt[1] = self.chamber_temp_filt[2]
    self.chamber_temp_filt[2] = self.chamber_temp_filt[3]
    # self.chamber_temp_filt[3] =   (self.chamber_temp[0] + self.chamber_temp[3] + 3 * (self.chamber_temp[1] + self.chamber_temp[2]))/3.430944333e+04
    #              + ( 0.8818931306    * self.chamber_temp_filt[0]) + (  -2.7564831952     * self.chamber_temp_filt[1]) + ( 2.8743568927 * self.chamber_temp_filt[2] );
    #  Moving average filter
    self.chamber_temp_filt[3] = (self.chamber_temp[0] + self.chamber_temp[1] + self.chamber_temp[2] + self.chamber_temp[3])/4;

    self.beer_temp[0] = self.beer_temp[1]
    self.beer_temp[1] = self.beer_temp[2]
    self.beer_temp[2] = self.beer_temp[3];
    self.beer_temp[3] = app.brewapp_thermometer_last[self.fermenter()["sensorid"]]

    #  Butterworth filter with cutoff frequency 0.01*sample frequency (FS=0.1Hz)
    self.beer_temp_filt[0] = self.beer_temp_filt[1]
    self.beer_temp_filt[1] = self.beer_temp_filt[2]
    self.beer_temp_filt[2] = self.beer_temp_filt[3];
    # self.beer_temp_filt[3] =   (self.beer_temp[0] + self.beer_temp[3] + 3 * (self.beer_temp[1] + self.beer_temp[2]))/3.430944333e+04
    # + ( 0.8818931306    * self.beer_temp_filt[0]) + (  -2.7564831952     * self.beer_temp_filt[1]) + ( 2.8743568927 * self.beer_temp_filt[2] );
   # Moving average filter
    self.beer_temp_filt[3] = (self.beer_temp[0] + self.beer_temp[1] + self.beer_temp[2] + self.beer_temp[3])/4;

  def update_outputs(self):
    if self.state == "COOLING":
      self.heat(False)
      self.cool(True)
    elif self.state == "HEATING":
      self.heat(True)
      self.cool(False)
    else:
      self.heat(False)
      self.cool(False)

  def detect_peaks(self):
    # detect peaks in fridge temperature to tune overshoot estimators
    if self.do_pos_peak_detect and self.state != "HEATING":
      if self.chamber_temp_filt[3] <= self.chamber_temp_filt[2] and self.chamber_temp_filt[2] >= self.chamber_temp_filt[1]: # maximum
        self.pos_peak=self.chamber_temp_filt[2];
        if self.pos_peak > self.chamber_setting_for_pos_peak_estimate + HEATING_TARGET_UPPER:
          # should not happen, estimated overshoot was too low, so adjust overshoot estimator
          self.heat_overshoot_estimator = self.heat_overshoot_estimator * (1.2 + min((self.pos_peak - (self.chamber_setting_for_pos_peak_estimate + HEATING_TARGET_UPPER)) * .03, 0.3))
          # saveSettings()
        if self.pos_peak < self.chamber_setting_for_pos_peak_estimate+HEATING_TARGET_LOWER:
          #should not happen, estimated overshoot was too high, so adjust overshoot estimator
          self.heat_overshoot_estimator = self.heat_overshoot_estimator*(0.8+max((self.pos_peak-(self.chamber_setting_for_pos_peak_estimate+HEATING_TARGET_LOWER))*.03,-0.3))
          # saveSettings();
        self.do_pos_peak_detect = False

      elif self.time_since_heating() > datetime.timedelta(minutes=10) and self.time_since_cooling() > datetime.timedelta(minutes=15) and self.chamber_temp_filt[3] < self.chamber_setting_for_pos_peak_estimate+HEATING_TARGET_LOWER:
        #there was no peak, but the estimator is too low. This is the heat, then drift up situation.
          self.pos_peak=self.chamber_temp_filt[3];
          self.heat_overshoot_estimator=self.heat_overshoot_estimator*(0.8+max((self.pos_peak-(self.chamber_setting_for_pos_peak_estimate+HEATING_TARGET_LOWER))*.03,-0.3));
          # saveSettings();
          self.do_pos_peak_detect = False


    if self.do_neg_peak_detect and self.state!="COOLING":
      if self.chamber_temp_filt[3] >= self.chamber_temp_filt[2] and self.chamber_temp_filt[2] <= self.chamber_temp_filt[1]: # minimum
        self.neg_peak=self.chamber_temp_filt[2];
        if self.neg_peak<self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_LOWER:
          #should not happen, estimated overshoot was too low, so adjust overshoot estimator
          self.cool_overshoot_estimator=self.cool_overshoot_estimator*(1.2+min(((self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_LOWER)-self.neg_peak)*.03,0.3));
          # saveSettings();

        if self.neg_peak>self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_UPPER:
          #should not happen, estimated overshoot was too high, so adjust overshoot estimator
          self.cool_overshoot_estimator=self.cool_overshoot_estimator*(0.8+max(((self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_UPPER)-self.neg_peak)*.03,-0.3));
          # saveSettings();

        self.do_neg_peak_detect = False

      elif self.time_since_cooling() > datetime.timedelta(minutes=30) and self.time_since_heating() > datetime.timedelta(minutes=30) and self.chamber_temp_filt[3] > self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_UPPER:
        #there was no peak, but the estimator is too low. This is the cool, then drift down situation.
          self.neg_peak=self.chamber_temp_filt[3];
          self.cool_overshoot_estimator=self.cool_overshoot_estimator*(0.8+max((self.neg_peak-(self.chamber_setting_for_neg_peak_estimate+COOLING_TARGET_UPPER))*.03,-0.3));
          # saveSettings();
          self.do_neg_peak_detect = False

  def time_since_cooling(self):
    return datetime.datetime.utcnow() - self.last_cool_time

  def time_since_heating(self):
    return datetime.datetime.utcnow() - self.last_heat_time

  def time_since_idle(self):
    return datetime.datetime.utcnow() - self.last_idle_time

  def update_slope(self):
    self.beer_temp_history[self.beer_temp_history_index]=self.beer_temp_filt[3];
    self.beer_slope = self.beer_temp_history[self.beer_temp_history_index]-self.beer_temp_history[(self.beer_temp_history_index+1) % 30]
    self.beer_temp_history_index = (self.beer_temp_history_index+1) % 30

  def heat(self, on_off):
    heater_id = self.fermenter()["heaterid"] if type(self.fermenter()["heaterid"]) is int else None
    if heater_id is not None:
      if on_off:
        switchOn(heater_id)
      else:
        switchOff(heater_id)

  def cool(self, on_off):
    cooler_id = self.fermenter()["coolerid"] if type(self.fermenter()["coolerid"]) is int else None
    if cooler_id is not None:
      if on_off:
        switchOn(cooler_id)
      else:
        switchOff(cooler_id)

def constrain(n, minn, maxn):
  return max(min(maxn, n), minn)

def min_temp():
  if app.brewapp_config.get("UNIT", "C") == "F":
    return 38.0
  else:
    return 3.5

def max_temp():
  if app.brewapp_config.get("UNIT", "C") == "F":
    return 300.0
  else:
    return 150.0
