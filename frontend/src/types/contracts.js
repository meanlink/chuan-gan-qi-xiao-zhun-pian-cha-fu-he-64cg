/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string,explicit_scale_rows:number,legacy_celsius_rows:number,normalized_rows:number,storage_generation:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean,input_snapshot:Object}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{status:string,batch_size:number,total_records:number,migrated_records:number,failed_records:number,version:number,verification:Object,batch_counts:Object,datasets:Array}} MigrationState */
export {};
