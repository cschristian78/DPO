/*
    Dynamic Portfolio Optimization (BMS_DPO)

    Purpose
    -------
    Measure how each account, policy, or location contributes to portfolio
    tail loss. The app reads Moody's RMS catastrophe-model loss output from
    an RDM and stores the rebuilt PML (EXCL) and marginal difference (DIFF).

    An analysis is the top-level record. Settings under that analysis choose
    the loss level, loss perspective, and EP points. A run executes one
    settings row against RDM loss data.

    In scope now
    -------------
    RDM loss data only. ClientID is the BMS_CMS.dbo.tClient key for an
    Active client. EDM name, portfolio, RDM analysis name, and peril are
    copied from rdm_analysis when the analysis is saved. This database does
    not copy the RDM.

    Out of scope
    ------------
    Moody's RMS EDM reference data (exposure attributes) is a later expansion.
*/

SET NOCOUNT ON;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tLossLevel
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tLossLevel', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tLossLevel (
            LossLevelID   INT          NOT NULL,
            LossLevelName VARCHAR(32)  NOT NULL,
            CONSTRAINT PK_tLossLevel PRIMARY KEY CLUSTERED (LossLevelID),
            CONSTRAINT UQ_tLossLevel_LossLevelName UNIQUE (LossLevelName)
        );
    END;

    MERGE dbo.tLossLevel AS tgt
    USING (VALUES
        (1, 'Account'),
        (2, 'Policy'),
        (3, 'Location'),
        (4, 'Lob'),
        (5, 'Other')
    ) AS src (LossLevelID, LossLevelName)
      ON tgt.LossLevelID = src.LossLevelID
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (LossLevelID, LossLevelName)
        VALUES (src.LossLevelID, src.LossLevelName);
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tLossPerspective
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tLossPerspective', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tLossPerspective (
            LossPerspectiveID   INT         NOT NULL,
            LossPerspectiveCode VARCHAR(4)  NOT NULL,
            CONSTRAINT PK_tLossPerspective PRIMARY KEY CLUSTERED (LossPerspectiveID),
            CONSTRAINT UQ_tLossPerspective_LossPerspectiveCode UNIQUE (LossPerspectiveCode)
        );
    END;

    MERGE dbo.tLossPerspective AS tgt
    USING (VALUES
        (1, 'GU'),
        (2, 'GR'),
        (3, 'RL')
    ) AS src (LossPerspectiveID, LossPerspectiveCode)
      ON tgt.LossPerspectiveID = src.LossPerspectiveID
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (LossPerspectiveID, LossPerspectiveCode)
        VALUES (src.LossPerspectiveID, src.LossPerspectiveCode);
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tEpPoint
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tEpPoint', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tEpPoint (
            EpPointID     INT          IDENTITY(1,1) NOT NULL,
            ReturnPeriod  INT          NOT NULL,
            SortOrder     INT          NOT NULL,
            IsActive      BIT          NOT NULL
                CONSTRAINT DF_tEpPoint_IsActive DEFAULT (1),
            CONSTRAINT PK_tEpPoint PRIMARY KEY CLUSTERED (EpPointID),
            CONSTRAINT UQ_tEpPoint_ReturnPeriod UNIQUE (ReturnPeriod)
        );
    END;

    MERGE dbo.tEpPoint AS tgt
    USING (VALUES
        (5), (10), (25), (50), (75), (100),
        (130), (250), (500), (750), (1000)
    ) AS src (ReturnPeriod)
      ON tgt.ReturnPeriod = src.ReturnPeriod
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (ReturnPeriod, SortOrder, IsActive)
        VALUES (src.ReturnPeriod, src.ReturnPeriod, 1);
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tAnalysis
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tAnalysis', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tAnalysis (
            AnalysisID          INT            IDENTITY(1,1) NOT NULL,
            AnalysisName        NVARCHAR(255)  NOT NULL,
            AnalysisDescription NVARCHAR(1000) NULL,
            ClientID            INT            NOT NULL,
            EdmName             NVARCHAR(255)  NULL,
            PortfolioID         INT            NULL,
            RdmName             NVARCHAR(128)  NOT NULL,
            RdmAnalysisID       INT            NOT NULL,
            RdmAnalysisName     NVARCHAR(255)  NOT NULL,
            Peril               NVARCHAR(64)   NULL,
            CreatedAt           DATETIME2(0)   NOT NULL
                CONSTRAINT DF_tAnalysis_CreatedAt DEFAULT (SYSUTCDATETIME()),
            ModifiedAt          DATETIME2(0)   NOT NULL
                CONSTRAINT DF_tAnalysis_ModifiedAt DEFAULT (SYSUTCDATETIME()),
            CONSTRAINT PK_tAnalysis PRIMARY KEY CLUSTERED (AnalysisID)
        );
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tAnalysisSettings
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tAnalysisSettings', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tAnalysisSettings (
            AnalysisSettingsID  INT           IDENTITY(1,1) NOT NULL,
            AnalysisID          INT           NOT NULL,
            LossLevelID         INT           NOT NULL,
            LossPerspectiveID   INT           NOT NULL,
            CreatedAt           DATETIME2(0)  NOT NULL
                CONSTRAINT DF_tAnalysisSettings_CreatedAt DEFAULT (SYSUTCDATETIME()),
            CONSTRAINT PK_tAnalysisSettings PRIMARY KEY CLUSTERED (AnalysisSettingsID),
            CONSTRAINT FK_tAnalysisSettings_tAnalysis
                FOREIGN KEY (AnalysisID) REFERENCES dbo.tAnalysis (AnalysisID),
            CONSTRAINT FK_tAnalysisSettings_tLossLevel
                FOREIGN KEY (LossLevelID) REFERENCES dbo.tLossLevel (LossLevelID),
            CONSTRAINT FK_tAnalysisSettings_tLossPerspective
                FOREIGN KEY (LossPerspectiveID) REFERENCES dbo.tLossPerspective (LossPerspectiveID),
            CONSTRAINT UQ_tAnalysisSettings_AnalysisLevelPerspective
                UNIQUE (AnalysisID, LossLevelID, LossPerspectiveID)
        );
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tAnalysisEpPoint
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tAnalysisEpPoint', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tAnalysisEpPoint (
            AnalysisEpPointID   INT  IDENTITY(1,1) NOT NULL,
            AnalysisSettingsID  INT  NOT NULL,
            EpPointID           INT  NOT NULL,
            CONSTRAINT PK_tAnalysisEpPoint PRIMARY KEY CLUSTERED (AnalysisEpPointID),
            CONSTRAINT FK_tAnalysisEpPoint_tAnalysisSettings
                FOREIGN KEY (AnalysisSettingsID) REFERENCES dbo.tAnalysisSettings (AnalysisSettingsID),
            CONSTRAINT FK_tAnalysisEpPoint_tEpPoint
                FOREIGN KEY (EpPointID) REFERENCES dbo.tEpPoint (EpPointID),
            CONSTRAINT UQ_tAnalysisEpPoint_SettingsPoint
                UNIQUE (AnalysisSettingsID, EpPointID)
        );
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tAnalysisRun
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tAnalysisRun', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tAnalysisRun (
            AnalysisRunID       INT            IDENTITY(1,1) NOT NULL,
            AnalysisSettingsID  INT            NOT NULL,
            RunStatus           VARCHAR(20)    NOT NULL
                CONSTRAINT DF_tAnalysisRun_RunStatus DEFAULT ('Queued'),
            EventCount          INT            NULL,
            EntityCount         INT            NULL,
            ErrorMessage        NVARCHAR(4000) NULL,
            CreatedAt           DATETIME2(0)   NOT NULL
                CONSTRAINT DF_tAnalysisRun_CreatedAt DEFAULT (SYSUTCDATETIME()),
            StartedAt           DATETIME2(0)   NULL,
            CompletedAt         DATETIME2(0)   NULL,
            CONSTRAINT PK_tAnalysisRun PRIMARY KEY CLUSTERED (AnalysisRunID),
            CONSTRAINT FK_tAnalysisRun_tAnalysisSettings
                FOREIGN KEY (AnalysisSettingsID) REFERENCES dbo.tAnalysisSettings (AnalysisSettingsID),
            CONSTRAINT CK_tAnalysisRun_RunStatus
                CHECK (RunStatus IN ('Queued', 'Running', 'Complete', 'Failed'))
        );
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_CreateTable_tPmlResult
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.tPmlResult', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.tPmlResult (
            PmlResultID     BIGINT         IDENTITY(1,1) NOT NULL,
            AnalysisRunID   INT            NOT NULL,
            ResultType      CHAR(4)        NOT NULL,
            EntityID        NVARCHAR(255)  NOT NULL,
            ReturnPeriod    INT            NOT NULL,
            PmlValue        FLOAT          NOT NULL,
            CONSTRAINT PK_tPmlResult PRIMARY KEY CLUSTERED (PmlResultID),
            CONSTRAINT FK_tPmlResult_tAnalysisRun
                FOREIGN KEY (AnalysisRunID) REFERENCES dbo.tAnalysisRun (AnalysisRunID),
            CONSTRAINT UQ_tPmlResult_RunTypeEntityPeriod
                UNIQUE (AnalysisRunID, ResultType, EntityID, ReturnPeriod),
            CONSTRAINT CK_tPmlResult_ResultType
                CHECK (ResultType IN ('EXCL', 'DIFF'))
        );
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Dpo_Initialize
AS
BEGIN
    SET NOCOUNT ON;
    EXEC dbo.usp_CreateTable_tLossLevel;
    EXEC dbo.usp_CreateTable_tLossPerspective;
    EXEC dbo.usp_CreateTable_tEpPoint;
    EXEC dbo.usp_CreateTable_tAnalysis;
    EXEC dbo.usp_CreateTable_tAnalysisSettings;
    EXEC dbo.usp_CreateTable_tAnalysisEpPoint;
    EXEC dbo.usp_CreateTable_tAnalysisRun;
    EXEC dbo.usp_CreateTable_tPmlResult;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Analysis_Create
    @AnalysisName        NVARCHAR(255),
    @AnalysisDescription NVARCHAR(1000) = NULL,
    @ClientID            INT,
    @EdmName             NVARCHAR(255) = NULL,
    @PortfolioID         INT = NULL,
    @RdmName             NVARCHAR(128),
    @RdmAnalysisID       INT,
    @RdmAnalysisName     NVARCHAR(255),
    @Peril               NVARCHAR(64) = NULL,
    @AnalysisID          INT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO dbo.tAnalysis (
        AnalysisName, AnalysisDescription, ClientID, EdmName, PortfolioID,
        RdmName, RdmAnalysisID, RdmAnalysisName, Peril
    )
    VALUES (
        @AnalysisName, @AnalysisDescription, @ClientID, @EdmName, @PortfolioID,
        @RdmName, @RdmAnalysisID, @RdmAnalysisName, @Peril
    );

    SET @AnalysisID = SCOPE_IDENTITY();
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Analysis_Get
    @AnalysisID INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        AnalysisID, AnalysisName, AnalysisDescription, ClientID,
        EdmName, PortfolioID, RdmName, RdmAnalysisID, RdmAnalysisName, Peril,
        CreatedAt, ModifiedAt
    FROM dbo.tAnalysis
    WHERE AnalysisID = @AnalysisID;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Analysis_List
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        AnalysisID, AnalysisName, AnalysisDescription, ClientID,
        EdmName, PortfolioID, RdmName, RdmAnalysisID, RdmAnalysisName, Peril,
        CreatedAt, ModifiedAt
    FROM dbo.tAnalysis
    ORDER BY AnalysisID DESC;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Analysis_Update
    @AnalysisID          INT,
    @AnalysisName        NVARCHAR(255),
    @AnalysisDescription NVARCHAR(1000) = NULL,
    @ClientID            INT,
    @EdmName             NVARCHAR(255) = NULL,
    @PortfolioID         INT = NULL,
    @RdmName             NVARCHAR(128),
    @RdmAnalysisID       INT,
    @RdmAnalysisName     NVARCHAR(255),
    @Peril               NVARCHAR(64) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF NOT EXISTS (SELECT 1 FROM dbo.tAnalysis WHERE AnalysisID = @AnalysisID)
        THROW 50002, 'Analysis was not found.', 1;

    UPDATE dbo.tAnalysis
    SET AnalysisName = @AnalysisName,
        AnalysisDescription = @AnalysisDescription,
        ClientID = @ClientID,
        EdmName = @EdmName,
        PortfolioID = @PortfolioID,
        RdmName = @RdmName,
        RdmAnalysisID = @RdmAnalysisID,
        RdmAnalysisName = @RdmAnalysisName,
        Peril = @Peril,
        ModifiedAt = SYSUTCDATETIME()
    WHERE AnalysisID = @AnalysisID;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisSettings_Create
    @AnalysisID            INT,
    @LossLevelName         VARCHAR(32),
    @LossPerspectiveCode   VARCHAR(4),
    @AnalysisSettingsID    INT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @LossLevelID INT;
    DECLARE @LossPerspectiveID INT;

    SELECT @LossLevelID = LossLevelID
    FROM dbo.tLossLevel
    WHERE LossLevelName = @LossLevelName;

    SELECT @LossPerspectiveID = LossPerspectiveID
    FROM dbo.tLossPerspective
    WHERE LossPerspectiveCode = @LossPerspectiveCode;

    IF @LossLevelID IS NULL OR @LossPerspectiveID IS NULL
        THROW 50001, 'Loss level or loss perspective is not recognized.', 1;

    IF NOT EXISTS (SELECT 1 FROM dbo.tAnalysis WHERE AnalysisID = @AnalysisID)
        THROW 50002, 'Analysis was not found.', 1;

    INSERT INTO dbo.tAnalysisSettings (AnalysisID, LossLevelID, LossPerspectiveID)
    VALUES (@AnalysisID, @LossLevelID, @LossPerspectiveID);

    SET @AnalysisSettingsID = SCOPE_IDENTITY();

    INSERT INTO dbo.tAnalysisEpPoint (AnalysisSettingsID, EpPointID)
    SELECT @AnalysisSettingsID, EpPointID
    FROM dbo.tEpPoint
    WHERE IsActive = 1;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisSettings_List
    @AnalysisID INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        s.AnalysisSettingsID,
        s.AnalysisID,
        l.LossLevelName,
        p.LossPerspectiveCode,
        s.CreatedAt
    FROM dbo.tAnalysisSettings AS s
    INNER JOIN dbo.tLossLevel AS l
        ON l.LossLevelID = s.LossLevelID
    INNER JOIN dbo.tLossPerspective AS p
        ON p.LossPerspectiveID = s.LossPerspectiveID
    WHERE s.AnalysisID = @AnalysisID
    ORDER BY s.AnalysisSettingsID;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisSettings_Update
    @AnalysisSettingsID    INT,
    @LossLevelName         VARCHAR(32),
    @LossPerspectiveCode   VARCHAR(4)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @AnalysisID INT;
    DECLARE @LossLevelID INT;
    DECLARE @LossPerspectiveID INT;

    SELECT @AnalysisID = AnalysisID
    FROM dbo.tAnalysisSettings
    WHERE AnalysisSettingsID = @AnalysisSettingsID;

    IF @AnalysisID IS NULL
        THROW 50005, 'Analysis settings were not found.', 1;

    SELECT @LossLevelID = LossLevelID
    FROM dbo.tLossLevel
    WHERE LossLevelName = @LossLevelName;

    SELECT @LossPerspectiveID = LossPerspectiveID
    FROM dbo.tLossPerspective
    WHERE LossPerspectiveCode = @LossPerspectiveCode;

    IF @LossLevelID IS NULL OR @LossPerspectiveID IS NULL
        THROW 50001, 'Loss level or loss perspective is not recognized.', 1;

    IF EXISTS (
        SELECT 1
        FROM dbo.tAnalysisSettings
        WHERE AnalysisID = @AnalysisID
          AND LossLevelID = @LossLevelID
          AND LossPerspectiveID = @LossPerspectiveID
          AND AnalysisSettingsID <> @AnalysisSettingsID
    )
        THROW 50006, 'That loss level and loss perspective are already saved for this analysis.', 1;

    UPDATE dbo.tAnalysisSettings
    SET LossLevelID = @LossLevelID,
        LossPerspectiveID = @LossPerspectiveID
    WHERE AnalysisSettingsID = @AnalysisSettingsID;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_EpPoint_List
AS
BEGIN
    SET NOCOUNT ON;

    SELECT EpPointID, ReturnPeriod, SortOrder
    FROM dbo.tEpPoint
    WHERE IsActive = 1
    ORDER BY SortOrder, ReturnPeriod;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_EpPoint_Add
    @ReturnPeriod INT,
    @EpPointID    INT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    IF @ReturnPeriod <= 0
        THROW 50003, 'Return period must be greater than zero.', 1;

    SELECT @EpPointID = EpPointID
    FROM dbo.tEpPoint
    WHERE ReturnPeriod = @ReturnPeriod;

    IF @EpPointID IS NULL
    BEGIN
        INSERT INTO dbo.tEpPoint (ReturnPeriod, SortOrder, IsActive)
        VALUES (@ReturnPeriod, @ReturnPeriod, 1);
        SET @EpPointID = SCOPE_IDENTITY();
    END
    ELSE
    BEGIN
        UPDATE dbo.tEpPoint
        SET IsActive = 1
        WHERE EpPointID = @EpPointID;
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_EpPoint_Remove
    @ReturnPeriod INT
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE dbo.tEpPoint
    SET IsActive = 0
    WHERE ReturnPeriod = @ReturnPeriod
      AND IsActive = 1;

    IF @@ROWCOUNT = 0
        THROW 50004, 'Active EP point was not found.', 1;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisEpPoint_List
    @AnalysisSettingsID INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT e.EpPointID, e.ReturnPeriod, e.SortOrder
    FROM dbo.tAnalysisEpPoint AS a
    INNER JOIN dbo.tEpPoint AS e
        ON e.EpPointID = a.EpPointID
    WHERE a.AnalysisSettingsID = @AnalysisSettingsID
    ORDER BY e.SortOrder, e.ReturnPeriod;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisEpPoint_Add
    @AnalysisSettingsID INT,
    @ReturnPeriod       INT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @EpPointID INT;

    EXEC dbo.usp_EpPoint_Add
        @ReturnPeriod = @ReturnPeriod,
        @EpPointID = @EpPointID OUTPUT;

    IF NOT EXISTS (
        SELECT 1
        FROM dbo.tAnalysisEpPoint
        WHERE AnalysisSettingsID = @AnalysisSettingsID
          AND EpPointID = @EpPointID
    )
    BEGIN
        INSERT INTO dbo.tAnalysisEpPoint (AnalysisSettingsID, EpPointID)
        VALUES (@AnalysisSettingsID, @EpPointID);
    END;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisEpPoint_Remove
    @AnalysisSettingsID INT,
    @ReturnPeriod       INT
AS
BEGIN
    SET NOCOUNT ON;

    DELETE a
    FROM dbo.tAnalysisEpPoint AS a
    INNER JOIN dbo.tEpPoint AS e
        ON e.EpPointID = a.EpPointID
    WHERE a.AnalysisSettingsID = @AnalysisSettingsID
      AND e.ReturnPeriod = @ReturnPeriod;

    IF @@ROWCOUNT = 0
        THROW 50005, 'EP point is not on this analysis.', 1;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisRun_Create
    @AnalysisSettingsID INT,
    @AnalysisRunID      INT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    IF NOT EXISTS (
        SELECT 1 FROM dbo.tAnalysisSettings WHERE AnalysisSettingsID = @AnalysisSettingsID
    )
        THROW 50006, 'Analysis settings were not found.', 1;

    INSERT INTO dbo.tAnalysisRun (AnalysisSettingsID)
    VALUES (@AnalysisSettingsID);

    SET @AnalysisRunID = SCOPE_IDENTITY();
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisRun_SetStatus
    @AnalysisRunID  INT,
    @RunStatus      VARCHAR(20),
    @EventCount     INT = NULL,
    @EntityCount    INT = NULL,
    @ErrorMessage   NVARCHAR(4000) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE dbo.tAnalysisRun
    SET RunStatus = @RunStatus,
        EventCount = COALESCE(@EventCount, EventCount),
        EntityCount = COALESCE(@EntityCount, EntityCount),
        ErrorMessage = @ErrorMessage,
        StartedAt = CASE
            WHEN @RunStatus = 'Running' AND StartedAt IS NULL THEN SYSUTCDATETIME()
            ELSE StartedAt
        END,
        CompletedAt = CASE
            WHEN @RunStatus IN ('Complete', 'Failed') THEN SYSUTCDATETIME()
            ELSE CompletedAt
        END
    WHERE AnalysisRunID = @AnalysisRunID;

    IF @@ROWCOUNT = 0
        THROW 50007, 'Analysis run was not found.', 1;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisRun_Get
    @AnalysisRunID INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        r.AnalysisRunID,
        r.AnalysisSettingsID,
        a.AnalysisID,
        a.AnalysisName,
        l.LossLevelName,
        p.LossPerspectiveCode,
        r.RunStatus,
        r.EventCount,
        r.EntityCount,
        r.ErrorMessage,
        r.CreatedAt,
        r.StartedAt,
        r.CompletedAt
    FROM dbo.tAnalysisRun AS r
    INNER JOIN dbo.tAnalysisSettings AS s
        ON s.AnalysisSettingsID = r.AnalysisSettingsID
    INNER JOIN dbo.tAnalysis AS a
        ON a.AnalysisID = s.AnalysisID
    INNER JOIN dbo.tLossLevel AS l
        ON l.LossLevelID = s.LossLevelID
    INNER JOIN dbo.tLossPerspective AS p
        ON p.LossPerspectiveID = s.LossPerspectiveID
    WHERE r.AnalysisRunID = @AnalysisRunID;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_AnalysisRun_List
    @AnalysisID INT = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        r.AnalysisRunID,
        r.AnalysisSettingsID,
        a.AnalysisID,
        a.AnalysisName,
        l.LossLevelName,
        p.LossPerspectiveCode,
        r.RunStatus,
        r.EventCount,
        r.EntityCount,
        r.CreatedAt,
        r.CompletedAt
    FROM dbo.tAnalysisRun AS r
    INNER JOIN dbo.tAnalysisSettings AS s
        ON s.AnalysisSettingsID = r.AnalysisSettingsID
    INNER JOIN dbo.tAnalysis AS a
        ON a.AnalysisID = s.AnalysisID
    INNER JOIN dbo.tLossLevel AS l
        ON l.LossLevelID = s.LossLevelID
    INNER JOIN dbo.tLossPerspective AS p
        ON p.LossPerspectiveID = s.LossPerspectiveID
    WHERE @AnalysisID IS NULL OR a.AnalysisID = @AnalysisID
    ORDER BY r.AnalysisRunID DESC;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_PmlResult_Clear
    @AnalysisRunID INT,
    @ResultType    CHAR(4)
AS
BEGIN
    SET NOCOUNT ON;

    IF @ResultType NOT IN ('EXCL', 'DIFF')
        THROW 50008, 'Result type must be EXCL or DIFF.', 1;

    DELETE FROM dbo.tPmlResult
    WHERE AnalysisRunID = @AnalysisRunID
      AND ResultType = @ResultType;
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_PmlResult_Insert
    @AnalysisRunID INT,
    @ResultType    CHAR(4),
    @EntityID      NVARCHAR(255),
    @ReturnPeriod  INT,
    @PmlValue      FLOAT
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO dbo.tPmlResult (
        AnalysisRunID, ResultType, EntityID, ReturnPeriod, PmlValue
    )
    VALUES (
        @AnalysisRunID, @ResultType, @EntityID, @ReturnPeriod, @PmlValue
    );
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_PmlResult_List
    @AnalysisRunID INT,
    @ResultType    CHAR(4)
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        EntityID,
        ReturnPeriod,
        PmlValue
    FROM dbo.tPmlResult
    WHERE AnalysisRunID = @AnalysisRunID
      AND ResultType = @ResultType
    ORDER BY
        CASE WHEN EntityID = N'PORTFOLIO' THEN 0 ELSE 1 END,
        EntityID,
        ReturnPeriod;
END;
GO

EXEC dbo.usp_Dpo_Initialize;
GO

CREATE OR ALTER PROCEDURE dbo.usp_Client_MatchRdmDatabase
    @RdmName NVARCHAR(128)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @underscore INT = CHARINDEX(N'_', @RdmName);
    DECLARE @prefix NVARCHAR(128) = CASE
        WHEN @underscore > 1 THEN LEFT(@RdmName, @underscore - 1)
        ELSE NULL
    END;

    SELECT TOP (1)
        ClientID,
        ClientName,
        ClientShortName
    FROM BMS_CMS.dbo.tClient
    WHERE ClientStatus = 'Active'
      AND ClientShortName = @prefix;
END;
GO
