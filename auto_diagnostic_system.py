# auto_diagnostic.py
# Расширение для автоматической диагностики принтера в Klipper

import logging

class AutoDiagnostic:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.toolhead = None
        self.results = {}
        # Загрузка параметров конфигурации
        self.temps = {
            'extruder': config.getfloat('extruder_temp', 200.0),
            'extruder1': config.getfloat('extruder1_temp', 200.0, minval=0),
            'heater_bed': config.getfloat('bed_temp', 60.0, minval=0),
            'chamber': config.getfloat('chamber_temp', 50.0, minval=0),
            'Chamber': config.getfloat('Chamber_temp', 50.0, minval=0)  # Двойное название
        }
        self.park_positions = {
            'X': config.getfloat('park_x', 10.0),
            'Y': config.getfloat('park_y', 10.0),
            'Z': config.getfloat('park_z', 10.0)
        }
        self.fan_check_drop = config.getfloat('fan_temp_drop', 3.5, minval=0.5)
        self.fan_check_time = config.getfloat('fan_check_time', 10.0, minval=1.0)
        # Регистрация команды G-кода
        self.gcode.register_command(
            'AUTO_DIAGNOSTIC',
            self.cmd_AUTO_DIAGNOSTIC,
            desc=self.cmd_AUTO_DIAGNOSTIC_help
        )
    cmd_AUTO_DIAGNOSTIC_help = "Запуск автоматической диагностики принтера"

    def cmd_AUTO_DIAGNOSTIC(self, gcmd):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.results = {
            'heaters': {}, 'parking': {},
            'leveling': {}, 'fan_check': None,
            'skip_leveling': False
        }
        try:
            self.gcode.respond_info("=== ЗАПУСК АВТОДИАГНОСТИКИ ===")
            self._heaters_check()
            self._park_axes()
            if not self.results['skip_leveling']:
                self._bed_leveling()
            self._fan_check()
        except Exception as e:
            logging.exception("Критическая ошибка диагностики")
            self.gcode.respond_error(f"Критическая ошибка: {str(e)}")
        finally:
            self._finalize()
    def _heaters_check(self):
        # Проверяем нагреватели в определенном порядке
        heaters_order = [
            ('heater_bed', self.temps['heater_bed']),
            ('chamber', self.temps['chamber']),
            ('Chamber', self.temps['Chamber']),  # Альтернативное название
            ('extruder', self.temps['extruder']),
            ('extruder1', self.temps['extruder1'])
        ]
        for heater_name, target_temp in heaters_order:
            if target_temp <= 0:
                self.results['heaters'][heater_name] = "Пропущен"
                continue
            try:
                self.gcode.respond_info(f"Нагрев {heater_name} до {target_temp}°C...")
                # Установка температуры через G-код
                self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET={target_temp}")
                # Ожидание достижения температуры
                self._wait_for_temperature(heater_name, target_temp)
                self.results['heaters'][heater_name] = "Успех"
                self.gcode.respond_info(f"{heater_name} достиг {target_temp}°C")
                # Немедленно отключаем стол и камеру после нагрева
                if heater_name in ['heater_bed', 'chamber', 'Chamber']:
                    self.gcode.respond_info(f"Отключение {heater_name}...")
                    self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET=0")
            except Exception as e:
                msg = f"Ошибка: {str(e)}"
                self.results['heaters'][heater_name] = msg
                logging.error(f"Heater {heater_name} error: {e}")
    def _wait_for_temperature(self, heater_name, target):
        tolerance = 2.0
        toolhead = self.printer.lookup_object('toolhead')
        last_temp = 0
        start_time = self.reactor.monotonic()
        start_time_progress = start_time
        # Получаем объект нагревателя для мониторинга температуры
        try:
            heater = self.printer.lookup_object(heater_name)
        except:
            heater = None
        while True:
            current_temp = 0
            if heater:
                try:
                    # Попытка получить температуру
                    current_temp, _ = heater.get_temp()
                except:
                    try:
                        # Альтернативный метод
                        status = heater.get_status(self.reactor.monotonic())
                        current_temp = status['temperature']
                    except:
                        pass
            toolhead.get_last_move_time()
            self.reactor.pause(self.reactor.monotonic() + 0.1)
            if current_temp >= target - tolerance:
                return
            if self.reactor.monotonic() > start_time + 300:
                raise Exception("Таймаут нагрева")
            if abs(current_temp - last_temp) > 0.1:
                last_temp = current_temp
                start_time_progress = self.reactor.monotonic()
            elif self.reactor.monotonic() > start_time_progress + 30:
                raise Exception("Нет прогресса нагрева")
    def _park_axes(self):
        axes = ['X', 'Y', 'Z']
        self.results['parking'] = {}
        try:
            # Парковка всех осей
            self.gcode.respond_info("Парковка всех осей (G28)...")
            self.gcode.run_script_from_command("G28")
            self.results['parking']['ALL'] = "Успех"
            # Перемещение в указанные позиции
            for axis in axes:
                try:
                    pos = self.park_positions[axis]
                    if pos <= 0:
                        continue
                    self.gcode.respond_info(f"Перемещение оси {axis} в позицию {pos}...")
                    self.gcode.run_script_from_command(f"G1 {axis}{pos} F6000")
                    self.results['parking'][axis] = "Успех"
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                except self.printer.command_error as e:
                    msg = f"Ошибка перемещения: {str(e)}"
                    self.results['parking'][axis] = msg
                    self.gcode.respond_error(f"Ошибка перемещения {axis}: {str(e)}")
                    if axis in ['X', 'Y']:
                        self.results['skip_leveling'] = True
                        self.gcode.respond_error("Пропуск G34/G29 из-за ошибки перемещения!")
                except Exception as e:
                    msg = f"Критическая ошибка: {str(e)}"
                    self.results['parking'][axis] = msg
                    self.gcode.respond_error(f"Критическая ошибка {axis}: {str(e)}")
                    self.results['skip_leveling'] = True
                    raise
        except self.printer.command_error as e:
            msg = f"Ошибка парковки: {str(e)}"
            self.results['parking']['ALL'] = msg
            self.gcode.respond_error(f"Ошибка парковки: {str(e)}")
            self.results['skip_leveling'] = True
        except Exception as e:
            msg = f"Критическая ошибка парковки: {str(e)}"
            self.results['parking']['ALL'] = msg
            self.gcode.respond_error(f"Критическая ошибка парковки: {str(e)}")
            self.results['skip_leveling'] = True
            raise
    def _bed_leveling(self):
        self.results['leveling'] = {}
        try:
            self.gcode.respond_info("Выполнение G34 (выравнивание стола)...")
            self.gcode.run_script_from_command("G34")
            self.results['leveling']['G34'] = "Успех"
        except self.printer.command_error as e:
            self.results['leveling']['G34'] = f"Ошибка: {str(e)}"
            self.gcode.respond_error(f"G34 error: {e}")
        except Exception as e:
            self.results['leveling']['G34'] = f"Критическая ошибка: {str(e)}"
            self.gcode.respond_error(f"Критическая ошибка G34: {e}")
            raise
        try:
            self.gcode.respond_info("Выполнение G29 (создание карты стола)...")
            self.gcode.run_script_from_command("G29")
            self.results['leveling']['G29'] = "Успех"
        except self.printer.command_error as e:
            self.results['leveling']['G29'] = f"Ошибка: {str(e)}"
            self.gcode.respond_error(f"G29 error: {e}")
        except Exception as e:
            self.results['leveling']['G29'] = f"Критическая ошибка: {str(e)}"
            self.gcode.respond_error(f"Критическая ошибка G29: {e}")
            raise
    def _fan_check(self):
        try:
            # Нагрев экструдера
            target_temp = self.temps['extruder']
            self.gcode.respond_info(f"Нагрев экструдера до {target_temp}°C для проверки вентилятора...")
            self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={target_temp}")
            self._wait_for_temperature('extruder', target_temp)
            # Получение температуры экструдера
            try:
                extruder = self.printer.lookup_object('extruder')
                start_temp, _ = extruder.get_temp()
            except:
                try:
                    extruder_heater = self.printer.lookup_object('extruder').heater
                    start_temp, _ = extruder_heater.get_temp()
                except:
                    start_temp = target_temp  # Если не удалось получить температуру
            self.gcode.respond_info(f"Начальная температура: {start_temp:.1f}°C")
            # Включение вентилятора
            self.gcode.respond_info("Включение вентилятора на 100%")
            self.gcode.run_script_from_command("M106 S255")
            self.gcode.respond_info(f"Ожидание {self.fan_check_time} сек...")
            start_time = self.reactor.monotonic()
            toolhead = self.printer.lookup_object('toolhead')
            min_temp = start_temp
            while self.reactor.monotonic() < start_time + self.fan_check_time:
                toolhead.get_last_move_time()
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                # Получение текущей температуры
                try:
                    if extruder:
                        current_temp, _ = extruder.get_temp()
                except:
                    try:
                        if extruder_heater:
                            current_temp, _ = extruder_heater.get_temp()
                    except:
                        current_temp = start_temp
                if current_temp < min_temp:
                    min_temp = current_temp
            # Выключение вентилятора
            self.gcode.run_script_from_command("M106 S0")
            temp_drop = start_temp - min_temp
            self.gcode.respond_info(f"Мин. температура: {min_temp:.1f}°C, падение: {temp_drop:.1f}°C")
            if temp_drop >= self.fan_check_drop:
                result = f"Успех (падение: {temp_drop:.1f}°C)"
            else:
                result = f"Сбой (падение: {temp_drop:.1f}°C)"
            self.results['fan_check'] = result
            self.gcode.respond_info(f"Проверка вентилятора: {result}")
        except Exception as e:
            # Гарантируем выключение вентилятора при ошибке
            try:
                self.gcode.run_script_from_command("M106 S0")
            except:
                pass
            self.results['fan_check'] = f"Ошибка: {str(e)}"
            logging.error(f"Fan check error: {e}")
            self.gcode.respond_error(f"Ошибка проверки вентилятора: {str(e)}")
    def _finalize(self):
        self.gcode.respond_info("Охлаждение нагревателей...")
        # Выключение всех нагревателей
        heaters = ['extruder', 'extruder1', 'heater_bed', 'chamber', 'Chamber']
        for heater_name in heaters:
            try:
                self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET=0")
            except:
                pass
        # Выключение вентилятора
        try:
            self.gcode.run_script_from_command("M106 S0")
        except:
            pass
        report = ["=== ОТЧЕТ АВТОДИАГНОСТИКИ ==="]
        report.append("-- НАГРЕВАТЕЛИ --")
        for name, status in self.results.get('heaters', {}).items():
            if self.temps.get(name, 0) > 0:  # Показываем только активные
                report.append(f"{name.upper()}: {status}")
        report.append("-- ПАРКОВКА И ПЕРЕМЕЩЕНИЕ ОСЕЙ --")
        for axis, status in self.results.get('parking', {}).items():
            report.append(f"{axis}: {status}")
        if not self.results.get('skip_leveling', True):
            report.append("-- ВЫРАВНИВАНИЕ СТОЛА --")
            for cmd, status in self.results.get('leveling', {}).items():
                report.append(f"{cmd}: {status}")
        else:
            report.append("-- ВЫРАВНИВАНИЕ СТОЛА ПРОПУЩЕНО ИЗ-ЗА ОШИБОК ПЕРЕМЕЩЕНИЯ --")
        report.append("-- ПРОВЕРКА ВЕНТИЛЯТОРА --")
        report.append(f"Результат: {self.results.get('fan_check', 'Не выполнена')}")
        report.append("Диагностика завершена!")
        report.append("=======================")
        self.gcode.respond_info("\n".join(report))

def load_config(config):
    return AutoDiagnostic(config)