# *****************************************************************************
# Â© Copyright IBM Corp. 2018.  All Rights Reserved.
#
# This program and the accompanying materials
# are made available under the terms of the Apache V2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
# *****************************************************************************

import logging
import json
import re
import numpy as np
import sys
from .util import log_df_info
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype, is_string_dtype, is_datetime64_any_dtype

logger = logging.getLogger(__name__)


class CalcPipeline:
    '''
    A CalcPipeline executes a series of dataframe transformation stages.
    '''
    def __init__(self,stages = None,entity_type =None):
        self.logger = logging.getLogger('%s.%s' % (self.__module__, self.__class__.__name__))
        self.entity_type = entity_type
        self.set_stages(stages)
        self.log_pipeline_stages()
        
    def add_expression(self,name,expression):
        '''
        Add a new stage using an expression
        '''
        stage = PipelineExpression(name=name,expression=expression,
                                   entity_type=self.entity_type)
        self.add_stage(stage)
        
    def add_stage(self,stage):
        '''
        Add a new stage to a pipeline. A stage is Transformer or Aggregator.
        '''
        stage.set_entity_type(self.entity_type)
        self.stages.append(stage)
          
        
    def _extract_preload_stages(self):
        '''
        pre-load stages are special stages that are processed outside of the pipeline
        they execute before loading data into the pipeline
        return tuple containing list of preload stages and list of other stages to be processed
        
        also extract scd lookups. Place them on the entity.
        '''
        stages = []
        extracted_stages = []
        for s in self.stages:
            try:
                is_preload = s.is_preload
            except AttributeError:
                is_preload = False
            #extract preload stages
            if is_preload:
                msg = 'Extracted preload stage %s from pipeline' %s.__class__.__name__
                logger.debug(msg)
                extracted_stages.append(s)
            else:
                stages.append(s)
                
        return (extracted_stages,stages)
                        
    
    def _execute_preload_stages(self, start_ts = None, end_ts = None, entities = None, register= False):
        '''
        Extract and run preload stages
        Return remaining stages to process
        '''
        (preload_stages,stages) = self._extract_preload_stages()
        preload_item_names = []
        #if no dataframe provided, querying the source entity to get one
        for p in preload_stages:
            if not self.entity_type._is_preload_complete:
                msg = 'Stage %s :' %p.__class__.__name__
                status = p.execute(df=None,start_ts=start_ts,end_ts=end_ts,entities=entities)
                msg = '%s completed as pre-load. ' %p.__class__.__name__
                self.trace_append(msg)
                if register:
                    p.register(df=None)
                try:
                    preload_item_names.append(p.output_item)
                except AttributeError:
                    msg = 'Preload functions are expected to have an argument and property called output_item. This preload function is not defined correctly'
                    raise AttributeError (msg)
                if not status:
                    msg = 'Preload stage %s returned with status of False. Aborting execution. ' %p.__class__.__name__
                    self.trace_append(msg)
                    stages = []
                    break
        self.entity_type._is_preload_complete = True
        return(stages,preload_item_names)
    
    
    def _execute_data_sources(self,stages,
                                df,
                                start_ts=None,
                                end_ts=None,
                                entities=None,
                                to_csv=False,
                                register=False,
                                dropna = False):
        '''
        Extract and execute data source stages with a merge_method of replace.
        Identify other data source stages that add rows of data to the pipeline
        '''
        remaining_stages = []
        secondary_sources = []
        special_lookup_stages = []
        replace_count = 0
        for s in stages:
            try:
                is_data_source =  s.is_data_source
                merge_method = s.merge_method
            except AttributeError:
                is_data_source = False
                merge_method = None        
                
            try:
                is_scd_lookup = s.is_scd_lookup
            except AttributeError:
                is_scd_lookup = False
            else:
                self.entity_type._add_scd_pipeline_stage(s)

            try:
                is_custom_calendar = s.is_custom_calendar
            except AttributeError:
                is_custom_calendar = False
            else:
                self.entity_type.set_custom_calendar(s)
                  
            if is_data_source and merge_method == 'replace':
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True)
                msg = 'Replaced incoming dataframe with custom data source %s. ' %s.__class__.__name__
                self.trace_append(msg, df = df)
                
            elif is_data_source and merge_method == 'outer':
                '''
                A data source with a merge method of outer is considered a secondary source
                A secondary source can add rows of data to the pipeline.
                '''
                secondary_sources.append(s)
            elif is_scd_lookup or is_custom_calendar:
                special_lookup_stages.append(s)
            else:
                remaining_stages.append(s)
        if replace_count > 1:
            self.logger.warning("The pipeline has more than one custom source with a merge strategy of replace. The pipeline will only contain data from the last replacement")        
        
        #execute secondary data sources
        if len(secondary_sources) > 0:
            for s in secondary_sources:
                msg = 'Processing secondary data source %s. ' %s.__class__.__name__
                self.trace_append(msg)
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True)
        
        #exceute special lookup stages
        if not df.empty and len(special_lookup_stages) > 0:                
            for s in special_lookup_stages:
                msg = 'Processing special lookup stage %s. ' %s.__class__.__name__
                self.trace_append(msg)
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True) 
            
        return(df,remaining_stages)    
            
                
    def execute(self, df=None, to_csv=False, dropna=False, start_ts = None, end_ts = None, entities = None, preloaded_item_names=None,
                register = False):
        '''
        Execute the pipeline using an input dataframe as source.
        '''
        #preload may  have already taken place. if so pass the names of the items produced by stages that were executed prior to loading.
        if preloaded_item_names is None:
            preloaded_item_names = []
        msg = 'Executing pipeline with %s stages.' % len(self.stages)
        logger.debug(msg)            
        is_initial_transform = self.get_initial_transform_status()
        # A single execution can contain multiple CalcPipeline executions
        # An initial transform and one or more aggregation executions and post aggregation transforms
        # Behavior is different during initial transform
        if entities is None:
            entities = self.entity_type.get_entity_filter()
        start_ts_override = self.entity_type.get_start_ts_override()
        if start_ts_override is not None:
            start_ts = start_ts_override
        end_ts_override = self.entity_type.get_end_ts_override()            
        if end_ts_override is not None:
            end_ts = end_ts_override            
        if is_initial_transform:
            if not start_ts is None:
                msg = 'Start timestamp: %s.' % start_ts
                self.trace_append(msg)
            if not end_ts is None:
                msg = 'End timestamp: %s.' % end_ts
                self.trace_append(msg)                
            #process preload stages first if there are any
            (stages,preload_item_names) = self._execute_preload_stages(start_ts = start_ts, end_ts = end_ts, entities = entities,register=register)
            preloaded_item_names.extend(preload_item_names)
            if df is None:
                msg = 'No dataframe supplied for pipeline execution. Getting entity source data'
                logger.debug(msg)
                df = self.entity_type.get_data(start_ts=start_ts, end_ts = end_ts, entities = entities)            
            #Divide the pipeline into data retrieval stages and transformation stages. First look for
            #a primary data source. A primary data source will have a merge_method of 'replace'. This
            #implies that it replaces whatever data was fed into the pipeline as default entity data.
            (df,stages) = self._execute_data_sources (
                                                df = df,
                                                stages = stages,
                                                start_ts = start_ts,
                                                end_ts = end_ts,
                                                entities = entities,
                                                to_csv = to_csv,
                                                register = register,
                                                dropna =  dropna
                                                )
                          
        else:
            stages = []
            stages.extend(self.stages)
        if df is None:
            msg = 'Pipeline has no source dataframe'
            raise ValueError (msg)
        if to_csv:
            filename = 'debugPipelineSourceData.csv'
            df.to_csv(filename)
        if dropna:
            df = df.replace([np.inf, -np.inf], np.nan)
            df = df.dropna()
        # remove rows that contain all nulls ignore deviceid and timestamp
        if self.entity_type.get_param('_drop_all_null_rows'):
            exclude_cols = self.get_system_columns()
            exclude_cols.extend(self.entity_type.get_param('_custom_exclude_col_from_auto_drop_nulls'))
            msg = 'columns excluded when dropping null rows %s' %exclude_cols
            logger.debug(msg)
            subset = [x for x in df.columns if x not in exclude_cols]
            msg = 'columns considered when dropping null rows %s' %subset
            logger.debug(msg)
            for col in subset:
                count = df[col].count()
                msg = '%s count not null: %s' %(col,count)
                logger.debug(msg)
            df = df.dropna(how='all', subset = subset )
            self.log_df_info(df,'post drop all null rows')
        else:
            logger.debug('drop all null rows disabled')
        #add a dummy item to the dataframe for each preload stage
        #added as the ui expects each stage to contribute one or more output items
        for pl in preloaded_item_names:
            df[pl] = True
        for s in stages:
            if df.empty:
                self.logger.info('No data retrieved from all sources. Exiting pipeline execution')        
                break
                #skip this stage of it is not a secondary source             
            df = self._execute_stage(stage=s,
                                df = df,
                                start_ts = start_ts,
                                end_ts = end_ts,
                                entities = entities,
                                register = register,
                                to_csv = to_csv,
                                dropna = dropna,
                                abort_on_fail = True)
        if is_initial_transform:
            try:
                self.entity_type.write_unmatched_members(df)
            except Exception as e:
                msg = 'Error while writing unmatched members to dimension. See log.' 
                self.trace_append(msg,created_by = self)
                self.entity_type.raise_error(exception = e,abort_on_fail = False)
            self.mark_initial_transform_complete()

        return df
    
    
    def _execute_stage(self,stage,df,start_ts,end_ts,entities,register,to_csv,dropna, abort_on_fail): 
        try:
            abort_on_fail = stage._abort_on_fail
        except AttributeError:
            abort_on_fail = abort_on_fail
        try:
            name = stage.name
        except AttributeError:
            name = stage.__class__.__name__
        #check to see if incoming data has a conformed index, conform if needed
        try:
            df = stage.conform_index(df=df)
        except AttributeError:
            pass
        except KeyError as e:
            msg = 'KeyError while conforming index prior to execution of function %s. ' %name
            self.trace_append(msg,created_by = stage, df = df)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        #there are two signatures for the execute method
        msg = 'Stage %s :' % name
        self.trace_append(msg=msg,df=df)
        try:
            try:
                newdf = stage.execute(df=df,start_ts=start_ts,end_ts=end_ts,entities=entities)
            except TypeError:
                newdf = stage.execute(df=df)
        except AttributeError as e:
            self.trace_append('The function %s makes a reference to an object property that does not exist. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except SyntaxError as e:
            self.trace_append('The function %s contains a syntax error. If the function configuration includes a type-in expression, make sure that this expression is correct. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except (ValueError,TypeError) as e:
            self.trace_append('The function %s is operating on data that has an unexpected value or data type. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except NameError as e:
            self.trace_append('The function %s referred to an object that does not exist. You may be referring to data items in pandas expressions, ensure that you refer to them by name, ie: as a quoted string. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except BaseException as e:
            self.trace_append('The function %s failed to execute. ' %name, created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        #validate that stage has not violated any pipeline processing rules
        try:
            self.validate_df(df,newdf)
        except AttributeError:
            msg = 'Function %s has no validate_df method. Skipping validation of the dataframe' %name
            logger.debug(msg)
        if register:
            try:
                stage.register(df=df,new_df= newdf)
            except AttributeError as e:
                msg = 'Could not export %s as it has no register() method or because an AttributeError was raised during execution' %name
                logger.warning(msg)
                logger.warning(str(e))
        if dropna:
            newdf = newdf.replace([np.inf, -np.inf], np.nan)
            newdf = newdf.dropna()
        if to_csv:
            newdf.to_csv('debugPipelineOut_%s.csv' %stage.__class__.__name__)

        msg = 'Completed stage %s. ' %name
        self.trace_append(msg,created_by=stage, df = newdf)
        return newdf
    
    def get_custom_calendar(self):
        '''
        Get the optional custom calendar for the entity type
        '''
        return self.entity_type._custom_calendar
    
    def get_initial_transform_status(self):
        '''
        Determine whether initial transform stage is complete
        '''
        return self.entity_type._is_initial_transform    
    
    def get_input_items(self):
        '''
        Get the set of input items explicitly requested by each function
        Not all input items have to be specified as arguments to the function
        Some can be requested through this method
        '''
        inputs = set()
        for s in self.stages:
            try:
                inputs = inputs | s.get_input_items()
            except AttributeError:
                pass
            
        return inputs
    
    def get_scd_lookup_stages(self):
        '''
        Get the scd lookup stages for the entity type
        '''
        return self.entity_type._scd_stages
    
    def get_system_columns(self):
        '''
        Get a list of system columns for the entity type
        '''
        return self.entity_type._system_columns

    
    def log_df_info(self,df,msg,include_data=False):
        '''
        Log a debugging entry showing first row and index structure
        '''
        msg = log_df_info(df=df,msg=msg,include_data = include_data)
        return msg
    
    def log_pipeline_stages(self):
        '''
        log pipeline stage metadata
        '''
        msg = 'pipeline stages (initial_transform=%s) ' %self.entity_type._is_initial_transform
        for s in self.stages:
            msg = msg + s.__class__.__name__
            msg = msg + ' > '
        return msg
    
    def mark_initial_transform_complete(self):
        self.entity_type._is_initial_transform = False
        
    def publish(self):
        export = []
        for s in self.stages:
            if self.entity_type is None:
                source_name = None
            else:
                source_name = self.entity_type.name
            metadata  = { 
                    'name' : s.name ,
                    'args' : s._get_arg_metadata()
                    }
            export.append(metadata)
            
        response = self.entity_type.db.http_request(object_type = 'kpiFunctions',
                                        object_name = source_name,
                                        request = 'POST',
                                        payload = export)    
        return response
            
    
    
    def _raise_error(self,exception,msg, abort_on_fail = False):
        #kept this method to preserve compatibility when
        #moving raise_error to the EntityType
        self.entity_type().raise_error(
                exception = exception,
                msg = msg,
                abort_on_fail = abort_on_fail
                )

            
    def set_stages(self,stages):
        '''
        Replace existing stages with a new list of stages
        '''
        self.stages = []
        if not stages is None:
            if not isinstance(stages,list):
                stages = [stages]
            self.stages.extend(stages)
        for s in self.stages:
            try:
                s.set_entity_type(self.entity_type)
            except AttributeError:
                s._entity_type = self.entity_type
                
    def __str__(self):
        
        return self.__class__.__name__
            
    def trace_append(self,msg,created_by = None, log_method = None, **kwargs):
        '''
        Append to the trace information collected the entity type
        '''
        if created_by is None:
            created_by = self
        
        self.entity_type.trace_append(created_by=created_by,
                                      msg = msg,
                                      log_method=log_method,
                                      **kwargs)

    def validate_df(self, input_df, output_df):

        validation_result = {}
        validation_types = {}
        for (df, df_name) in [(input_df, 'input'), (output_df, 'output')]:
            validation_types[df_name] = {}
            for c in list(df.columns):
                try:
                    validation_types[df_name][df[c].dtype].add(c)
                except KeyError:
                    validation_types[df_name][df[c].dtype] = {c}

            validation_result[df_name] = {}
            validation_result[df_name]['row_count'] = len(df.index)
            validation_result[df_name]['columns'] = set(df.columns)
            is_str_0 = False
            try:
                if is_string_dtype(df.index.get_level_values(self.entity_type._df_index_entity_id)):
                    is_str_0 = True
            except KeyError:
                pass
            is_dt_1 = False
            try:
                if is_datetime64_any_dtype(df.index.get_level_values(self.entity_type._timestamp)):
                    is_dt_1 = True
            except KeyError:
                pass
            validation_result[df_name]['is_index_0_str'] = is_str_0
            validation_result[df_name]['is_index_1_datetime'] = is_dt_1

        if validation_result['input']['row_count'] == 0:
            logger.warning('Input dataframe has no rows of data')
        elif validation_result['output']['row_count'] == 0:
            logger.warning('Output dataframe has no rows of data')

        if not validation_result['input']['is_index_0_str']:
            logger.warning(
                'Input dataframe index does not conform. First part not a string called %s' % self.entity_type._df_index_entity_id)
        if not validation_result['output']['is_index_0_str']:
            logger.warning(
                'Output dataframe index does not conform. First part not a string called %s' % self.entity_type._df_index_entity_id)

        if not validation_result['input']['is_index_1_datetime']:
            logger.warning(
                'Input dataframe index does not conform. Second part not a string called %s' % self.entity_type._timestamp)
        if not validation_result['output']['is_index_1_datetime']:
            logger.warning(
                'Output dataframe index does not conform. Second part not a string called %s' % self.entity_type._timestamp)

        mismatched_type = False
        for dtype, cols in list(validation_types['input'].items()):
            try:
                missing = cols - validation_types['output'][dtype]
            except KeyError:
                mismatched_type = True
                msg = 'Output dataframe has no columns of type %s. Type has changed or column was dropped.' % dtype
            else:
                if len(missing) != 0:
                    msg = 'Output dataframe is missing columns %s of type %s. Either the type has changed or column was dropped' % (
                    missing, dtype)
                    mismatched_type = True
            if mismatched_type:
                logger.warning(msg)

        self.check_data_items_type(df=output_df, items=self.entity_type.get_data_items())

        return (validation_result, validation_types)

    def check_data_items_type(self, df, items):
        '''
        Check if dataframe columns type is equivalent to the data item that is defined in the metadata
        It checks the entire list of data items. Thus, depending where this code is executed, the dataframe might not be completed.
        An exception is generated if there are not incompatible types of matching items AND and flag throw_error is set to TRUE
        '''

        invalid_data_items = list()

        if df is not None:
            #logger.info('Dataframe types before type conciliation: \n')
            #logger.info(df.dtypes)

            for item in list(items.data_items):  # transform in list to iterate over it
                df_column = {}
                try:
                    data_item = items.get(item)  # back to the original dict to retrieve item object
                    df_column = df[data_item['name']]
                except KeyError:
                    #logger.debug('Data item %s is not part of the dataframe yet.' % item)
                    continue

                # check if it is Number
                if data_item['columnType'] == 'NUMBER':
                    if not is_numeric_dtype(df_column.values) or is_bool_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))

                        try:
                            df[data_item['name']] = df_column.astype('float64')  # try to convert to numeric
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is String
                if data_item['columnType'] == 'LITERAL':
                    if not is_string_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = df_column.astype('str')  # try to convert to string
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is Timestamp
                if data_item['columnType'] == 'TIMESTAMP':
                    if not is_datetime64_any_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = pd.to_datetime(df_column)  # try to convert to timestamp
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is Boolean
                if data_item['columnType'] == 'BOOLEAN':
                    if not is_bool_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = df_column.astype('bool')
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

        else:
            logger.info('Not possible to retrieve information from the data frame')

        if len(invalid_data_items) > 0:
            msg = 'Some data items could not have its type conciliated:'
            for item, df_type, data_type in invalid_data_items:
                msg += ('\n %s: df type is %s and data type is %s' % (item, df_type, data_type))
            logger.error(msg)
            raise Exception(msg)


class PipelineExpression(object):
    '''
    Create a new item from an expression involving other items
    '''
    def __init__(self, expression , name, entity_type):
        self.expression = expression
        self.name = name
        super().__init__()
        self.input_items = []
        self.entity_type = entity_type
                
    def execute(self, df):
        df = df.copy()
        self.infer_inputs(df)
        if '${' in self.expression:
            expr = re.sub(r"\$\{(\w+)\}", r"df['\1']", self.expression)
        else:
            expr = self.expression
        try:
            df[self.name] = eval(expr)
        except SyntaxError:
            msg = 'Syntax error while evaluating expression %s' %expr
            raise SyntaxError (msg)
        else:
            msg = 'Evaluated expression %s' %expr
            self.entity_type.trace_append(msg,df=df)
        return df

    def get_input_items(self):
        return self.input_items
    
    def infer_inputs(self,df):
        #get all quoted strings in expression
        possible_items = re.findall('"([^"]*)"', self.expression)
        possible_items.extend(re.findall("'([^']*)'", self.expression))
        self.input_items = [x for x in possible_items if x in list(df.columns)]       
            

            
          
