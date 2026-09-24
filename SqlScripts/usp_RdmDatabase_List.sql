/*
    Deploy on the Moody's SQL Server, in master.
    Lists database names that contain RDM.
*/
USE master;
GO

CREATE OR ALTER PROCEDURE dbo.usp_RdmDatabase_List
AS
BEGIN
    SET NOCOUNT ON;

    SELECT name AS RdmName
    FROM sys.databases
    WHERE name LIKE '%RDM%'
    ORDER BY name;
END;
GO
